import asyncio
import base64
import io
import json
import pathlib
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from threading import Thread
from typing import List

from aiocache import cached
import aiohttp
import aiomysql
import nbt.nbt as nbt
import requests
import toml

from constants import *


class ConfigError(Exception):
    def __init__(self, message):
        print("*----- Please configure the bot in 'config.toml' before running -----*")
        print(message)


@dataclass
class Auction:
    name: str
    price: int
    value: int
    extra_value: int
    id: str
    image: str
    json_object: json

    def time_ending(self):
        end_seconds = self.json_object['end'] / 1000 - time.time()
        return "{:d}m {:d}s".format(int(end_seconds // 60), int(end_seconds % 60))

    @staticmethod
    def decode_item(item):
        base64_decoded = base64.b64decode(item)
        file = nbt.NBTFile(fileobj=io.BytesIO(base64_decoded))['i'][0]
        return file, file['tag']['ExtraAttributes']['id'].value, file['Count'].value

    @staticmethod
    def format_uuid(uuid):
        return "{}-{}-{}-{}-{}".format(uuid[:8], uuid[8:12], uuid[12:16], uuid[16:20], uuid[20:32])

    @staticmethod
    def get_name(nbt_data):
        name = nbt_data['tag']['ExtraAttributes']['id'].value
        if name == "ENCHANTED_BOOK":
            # Track individual book prices
            if len(nbt_data['tag']['ExtraAttributes']['enchantments'].keys()) == 1:
                ench = nbt_data['tag']['ExtraAttributes']['enchantments'].keys()[0]
                if ench in ENCHANTS and nbt_data['tag']['ExtraAttributes']['enchantments'][ench].value >= \
                        ENCHANTS[ench]:
                    return ench.upper()
        elif name == "PET":
            pet_json = json.loads(nbt_data['tag']['ExtraAttributes']['petInfo'].value)
            rarity = pet_json['tier']
            if 'heldItem' in pet_json and pet_json['heldItem'] == "PET_ITEM_TIER_BOOST":
                rarity = "LEGENDARY" if rarity == "EPIC" else rarity
            return rarity + "_" + pet_json['type'] + "_PET"
        return name


class AuctionGrabber(Thread):
    with open("alias.json", "r") as alias_file:
        aliases = json.loads(alias_file.read())

    def __init__(self, config, event_loop):
        super().__init__()
        self.config = config
        self.key = self.config['api']['hypixel']
        self.db = None
        self.loop: asyncio.AbstractEventLoop = event_loop
        self.run_counter = 5
        self.last_update = 0

        if not config['api']['hypixel']:
            raise ConfigError("Ensure you have entered values for api keys")

        loop.run_until_complete(self.configure_database())

        hypixel = self.config['api']['hypixel']
        if requests.get(TOKEN_TEST.format(hypixel)).status_code != 404:
            raise ConfigError("Invalid Hypixel API Key or the API is currently down")

    async def configure_database(self):
        db_connection = await aiomysql.create_pool(host=self.config['db']['host'],
                                                   port=self.config['db']['port'],
                                                   user=self.config['db']['username'],
                                                   password=self.config['db']['password'],
                                                   db=self.config['db']['database'],
                                                   loop=self.loop, minsize=30, maxsize=60)
        # raise ConfigError("MySQL credentials are incorrect")
        self.db = db_connection
        async with self.db.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("CREATE TABLE IF NOT EXISTS auctions("
                                  "id VARCHAR(36) PRIMARY KEY,"
                                  "item_name VARCHAR(255) NOT NULL,"
                                  "price FLOAT NOT NULL,"
                                  "seller VARCHAR(36) NOT NULL,"
                                  "value FLOAT NOT NULL,"
                                  "ending TIMESTAMP NOT NULL,"
                                  "bin BOOL NOT NULL,"
                                  "bytes TEXT NOT NULL)")

                await cur.execute("CREATE TABLE IF NOT EXISTS price("
                                  "item_name VARCHAR(255) NOT NULL,"
                                  "price FLOAT NOT NULL,"
                                  "time_checked DATETIME NOT NULL DEFAULT NOW(),"
                                  "PRIMARY KEY (item_name, time_checked))")

                await cur.execute("CREATE TABLE IF NOT EXISTS aliases("
                                  "item_name VARCHAR(255) PRIMARY KEY,"
                                  "alias FLOAT NOT NULL)")

    def run(self):
        while True:
            self.loop.run_until_complete(self.receive_auctions())
            time.sleep(60)

    async def get_pages(self, max_page, session):
        tasks = [self.get_page(i, session) for i in range(max_page + 1)]
        auc = await asyncio.gather(*tasks)
        print("Gathered")
        if self.run_counter == 5 and auc:
            bazaar_tasks = [self.get_bazaar(session, item) for item in
                            ["RECOMBOBULATOR_3000", "HOT_POTATO_BOOK", "FUMING_POTATO_BOOK"]]

            prices = {tup[0]: tup[1] for tup in await asyncio.gather(*bazaar_tasks)}
            for d in auc:
                for k, v in d.items():
                    if k not in prices or prices[k] > v:
                        prices[k] = v
            async with self.db.acquire() as conn:
                async with conn.cursor() as cur:
                    for item_name, price in prices.keys():
                        if item_name.upper() in IGNORE:
                            continue
                        await cur.execute("INSERT INTO price(item_name, price) VALUES (%s, %s)", (item_name, price))

        return auc

    async def get_bazaar(self, session, item) -> tuple:
        prod_json = json.loads(await (await session.get(BAZAAR_ENDPOINT.format(self.key))).text())
        return item, prod_json['products'][item]['sell_summary'][0]['pricePerUnit']

    async def receive_auctions(self):
        async with aiohttp.ClientSession() as session:
            resp = await session.get(AUCTION_ENDPOINT.format(self.key, 0))
            if resp.status != 200:
                return
            page0 = json.loads(await resp.text())
            if page0['lastUpdated'] == self.last_update:
                return
            # AuctionGrabber.get_price.cache_clear()
            await self.get_pages(page0['totalPages'], session)
        self.last_update = page0['lastUpdated']
        self.run_counter = 0 if self.run_counter == 5 else self.run_counter + 1

    async def insert_auction(self, auc):
        if auc['end'] < (time.time() * 1000):
            return
        nbt_data, *_ = Auction.decode_item(auc['item_bytes'])
        value = await self.calculate_worth(nbt_data)
        price = max(auc['starting_bid'], auc['highest_bid_amount'])
        timestamp = auc['end'] // 1000
        return (auc['uuid'], Auction.get_name(nbt_data), price, auc['auctioneer'], value, timestamp,
                'bin' in auc and auc['bin'], auc['item_bytes'])

    def __del__(self):
        if self.db is not None:
            self.db.close()

    async def get_page(self, page: int, session: aiohttp.ClientSession) -> dict:
        print("getting page", page)
        try:
            auctions_json = json.loads(await (await session.get(AUCTION_ENDPOINT.format(self.key, page))).text())
            if 'auctions' not in auctions_json:
                return {}
            auctions = auctions_json['auctions']
            tasks = [self.insert_auction(auc) for auc in auctions]
            async with self.db.acquire() as conn:
                async with conn.cursor() as cur:
                    data = await asyncio.gather(*tasks)
                    await cur.executemany("INSERT INTO auctions VALUES (%s, %s, %s, %s, %s, FROM_UNIXTIME(%s), %s, %s)"
                                          " ON DUPLICATE KEY UPDATE price=VALUES(price), ending=VALUES(ending)",
                                          data)
                    await conn.commit()

            print("Page gotten", page)
            # await cur.execute("DELETE FROM auctions WHERE ending < NOW()")

            if self.run_counter == 5:
                page_prices = defaultdict(lambda: [])
                for auction in auctions:
                    # todo improve
                    if 'bin' not in auction or auction['bin'] is False or auction['end'] < time.time() * 1000:
                        continue
                    nbt_data, _, count = Auction.decode_item(auction['item_bytes'])

                    name = Auction.get_name(nbt_data)
                    page_prices[name].append(auction['starting_bid'] / count)
                d = {k: min(v) for k, v in page_prices.items()}
                print(d)
                return d
        except aiohttp.ServerDisconnectedError:
            return {}

        return {}
        # return [auc for auc in auctions if
        #         auc['uuid'] not in self.notified and (((time.time() + 30) * 1000 < auc['end'] < (
        #                 time.time() + self.config['options']['min-time']) * 1000) or ('bin' in auc and auc['bin']))]

    async def check_flip(self) -> List[Auction]:
        flips = []
        # j = json.loads(requests.get(PROFILE_ENDPOINT.format(self.key, "13283977ac354d92b950bc1fda73081d")).content)
        j = json.loads(requests.get(PROFILE_ENDPOINT.format(self.key, "ee4dfc679c4245c8a34e89cfa6c02d62")).content)

        # max_price = j['profile']['banking']['balance'] + j['profile']['members']['36e418428cce45ee878d82e2be986d49'][
        #     'coin_purse']
        # max_price = j['profile']['members']['36e418428cce45ee878d82e2be986d49'][
        #     'coin_purse']
        max_price = sum([int(j['profile']['members'][bal]['coin_purse']) for bal in j['profile']['members']])
        return flips

    @cached(ttl=60)
    async def get_price(self, item):
        async with self.db.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT price FROM price INNER JOIN ("
                                  "  SELECT item_name, MAX(time_checked) AS recent FROM price GROUP BY item_name) p_tbl"
                                  "  ON price.item_name = p_tbl.item_name AND time_checked = p_tbl.recent "
                                  "WHERE price.item_name=%s", (item,))
                price = await cur.fetchone()
                return 0 if price is None else price[0]

    async def calculate_worth(self, nbt_data) -> int:
        raw_name = nbt_data['tag']['ExtraAttributes']['id'].value
        # todo handle count
        name = Auction.get_name(nbt_data)

        # Modify price depending on other enchants
        extra_attributes = nbt_data['tag']['ExtraAttributes']
        value = await self.get_price(name)
        if 'hot_potato_count' in extra_attributes:
            hpb_count = extra_attributes['hot_potato_count'].value
            if hpb_count > 10:
                value += await self.get_price('HOT_POTATO_BOOK') * 10 + (hpb_count - 10) * await self.get_price(
                    'FUMING_POTATO_BOOK')

        if 'rarity_upgrades' in extra_attributes:
            value += await self.get_price('RECOMBOBULATOR_3000')

        if 'wood_singularity_count' in extra_attributes and extra_attributes['wood_singularity_count'] == 1:
            value += await self.get_price('WOOD_SINGULARITY')

        # Don't increase the value of enchants on dungeon items (can spawn with them)
        # or enchanted books since their enchant worth is already set
        if 'enchantments' in extra_attributes and not raw_name == "ENCHANTED_BOOK" and \
                (('originTag' in extra_attributes and not extra_attributes['originTag'].value == "UNKNOWN")
                 and 'DUNGEON' not in ' '.join([s.value for s in nbt_data['tag']['display']['Lore']])):
            for ench in extra_attributes['enchantments'].keys():
                if ench in ENCHANTS and ENCHANTS[ench] <= extra_attributes['enchantments'][ench].value:
                    if ench == "dragon_hunter" or ench.startswith("ultimate"):
                        # base 2 multiplication
                        value += 2 ** (extra_attributes['enchantments'][ench].value - 1) \
                                 * await self.get_price(ench)
                    else:
                        value += await self.get_price(ench)

        # image = 'https://sky.lea.moe/item/' + raw_name
        # if 'SkullOwner' in nbt_data['tag']:
        #     head_json = json.loads(base64.b64decode(
        #         nbt_data['tag']['SkullOwner']['Properties']['textures'][0]['Value'].value + "===").decode("utf-8"))
        #     image = 'https://sky.lea.moe/head/' + head_json['textures']['SKIN']['url'].split("texture/", 1)[1]
        #
        # auction_obj = Auction(id=Auction.format_uuid(item['uuid']), name=name, price=price,
        #                       extra_value=value, json_object=item, image=image,
        #                       value=self.min_price[name] + value * PERCENTAGE_VALUE)
        # price -= value  # Value of extras is decreased once added
        return value


# todo Deal with pet xp
# todo Deal with fabled / reforge stones
# todo Handle aliases better
# todo Move check flip into page loading to save time waiting for all endpoints
# todo Only show for player's money (web)


if __name__ == "__main__":
    path = pathlib.Path("config.toml")
    if not path.exists():
        with open("config.toml", "a+") as config_file:
            config_file.write(DEFAULT_CONFIG)
        print("*----- Please configure the bot in 'config.toml' before running -----*")
        sys.exit()
    with open("config.toml") as config_file:
        conf = toml.loads(config_file.read())

    loop = asyncio.get_event_loop()
    thread = AuctionGrabber(conf, loop)
    thread.start()
