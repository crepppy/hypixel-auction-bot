import asyncio
import base64
import io
import json
import pathlib
import sys
import time
from quart import Quart
import quart
from dataclasses import dataclass
from threading import Thread
# import uvloop
from aiocache import cached
import aiohttp
import aiomysql
from aiomysql import DictCursor
import nbt.nbt as nbt
import requests
import toml

from constants import *


# asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


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
        if name == "ENCHANTED_BOOK" and 'enchantments' in nbt_data['tag']['ExtraAttributes']:
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


async def configure_database():
    async with db.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("CREATE TABLE IF NOT EXISTS auctions("
                              "id VARCHAR(36) PRIMARY KEY,"
                              "item_name VARCHAR(255) NOT NULL,"
                              "price FLOAT NOT NULL,"
                              "seller VARCHAR(36) NOT NULL,"
                              "extra_value FLOAT NOT NULL,"
                              "ending TIMESTAMP NOT NULL,"
                              "bin BOOL NOT NULL,"
                              "bytes TEXT NOT NULL) ENGINE=INNODB")

            await cur.execute("CREATE TABLE IF NOT EXISTS price("
                              "item_name VARCHAR(255) NOT NULL,"
                              "price FLOAT NOT NULL DEFAULT 0,"
                              "time_checked DATETIME NOT NULL DEFAULT NOW(),"
                              "PRIMARY KEY (item_name, time_checked)) ENGINE=INNODB")

            await cur.execute("CREATE TABLE IF NOT EXISTS aliases("
                              "item_name VARCHAR(255) PRIMARY KEY,"
                              "alias FLOAT NOT NULL) ENGINE=INNODB")

            await cur.execute("DELIMITER $$ CREATE FUNCTION IF NOT EXISTS LatestPrice(i_name VARCHAR(255))"
                              "RETURNS FLOAT BEGIN "
                              "RETURN (SELECT price FROM price INNER JOIN ("
                              "  SELECT item_name, MAX(time_checked) AS recent FROM price GROUP BY item_name) p_tbl"
                              "  ON price.item_name = p_tbl.item_name AND time_checked = p_tbl.recent "
                              "WHERE price.item_name=i_name); END; $$ DELIMITER ;")


class AuctionGrabber(Thread):
    def __init__(self, config, event_loop):
        super().__init__()
        self.config = config
        self.key = self.config['api']['hypixel']
        self.loop: asyncio.AbstractEventLoop = event_loop
        self.run_counter = 5
        self.last_update = 0

        if not config['api']['hypixel']:
            raise ConfigError("Ensure you have entered values for api keys")

        loop.run_until_complete(configure_database())

        hypixel = self.config['api']['hypixel']
        if requests.get(TOKEN_TEST.format(hypixel)).status_code != 404:
            raise ConfigError("Invalid Hypixel API Key or the API is currently down")

    def run(self):
        while True:
            start = time.time()
            self.loop.run_until_complete(self.receive_auctions())
            print(time.time() - start)
            time.sleep(60 - (time.time() - self.last_update // 1000))

    async def get_pages(self, max_page, session):
        tasks = [self.get_page(i, session) for i in range(max_page + 1)]
        await asyncio.gather(*tasks)
        async with db.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM auctions WHERE ending < NOW()")

        print("Gathered")
        if self.run_counter == 5:
            bazaar_tasks = [self.get_bazaar(session, item) for item in
                            ["RECOMBOBULATOR_3000", "HOT_POTATO_BOOK", "FUMING_POTATO_BOOK"]]
            await asyncio.gather(*bazaar_tasks)
            async with db.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("INSERT INTO price(item_name, price)"
                                      "SELECT item_name, MIN(price) min_price FROM auctions"
                                      " WHERE bin = 1 GROUP BY item_name")

    async def get_bazaar(self, session, item):
        prod_json = json.loads(await (await session.get(BAZAAR_ENDPOINT.format(self.key))).text())
        async with db.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("INSERT INTO price(item_name, price) VALUES (%s, %s)",
                                  (item, prod_json['products'][item]['sell_summary'][0]['pricePerUnit']))

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

    async def get_page(self, page: int, session: aiohttp.ClientSession):
        print("getting page", page)
        try:
            auctions_json = json.loads(await (await session.get(AUCTION_ENDPOINT.format(self.key, page))).text())
            if 'auctions' not in auctions_json:
                return
            auctions = auctions_json['auctions']
            data = []
            for auc in auctions:
                nbt_data, *_ = Auction.decode_item(auc['item_bytes'])
                value = await self.calculate_worth(nbt_data)
                price = max(auc['starting_bid'], auc['highest_bid_amount'])
                timestamp = auc['end'] // 1000
                data.append((auc['uuid'], Auction.get_name(nbt_data), price, auc['auctioneer'], value, timestamp,
                             'bin' in auc and auc['bin'], auc['item_bytes']))
            async with db.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.executemany("INSERT INTO auctions VALUES (%s, %s, %s, %s, %s, FROM_UNIXTIME(%s), %s, %s)"
                                          " ON DUPLICATE KEY UPDATE price=VALUES(price), ending=VALUES(ending)",
                                          data)

            print("Page gotten", page)
            # await cur.execute("DELETE FROM auctions WHERE ending < NOW()")
        except aiohttp.ServerDisconnectedError:
            pass

    @staticmethod
    @cached(ttl=300)
    async def get_price(item):
        async with db.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT LatestPrice(%s)", (item,))
                price = await cur.fetchone()
                return 0 if price is None else price[0]

    async def calculate_worth(self, nbt_data) -> int:
        raw_name = nbt_data['tag']['ExtraAttributes']['id'].value
        # todo handle count

        # Modify price depending on other enchants
        extra_attributes = nbt_data['tag']['ExtraAttributes']
        value = 0
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

path = pathlib.Path("config.toml")
if not path.exists():
    with open("config.toml", "a+") as config_file:
        config_file.write(DEFAULT_CONFIG)
    print("*----- Please configure the bot in 'config.toml' before running -----*")
    sys.exit()
with open("config.toml") as config_file:
    conf = toml.loads(config_file.read())

loop = asyncio.get_event_loop()
db = loop.run_until_complete(aiomysql.create_pool(
    host=conf['db']['host'],
    port=conf['db']['port'],
    user=conf['db']['username'],
    password=conf['db']['password'],
    db=conf['db']['database'],
    loop=loop, minsize=60, maxsize=100, autocommit=True))

app = Quart(__name__)


@app.route("/flips")
async def flips():
    buy = 'bin' in quart.request.args
    min_profit = quart.request.args.get("profit", default=100_000, type=int)
    max_money = quart.request.args.get("money")
    async with db.acquire() as conn:
        async with conn.cursor(DictCursor) as cur:
            await cur.execute(
                "SELECT auctions.id,"
                "       auctions.item_name,"
                "       auctions.price,"
                "       auctions.seller,"
                "       auctions.ending,"
                "       auctions.bin,"
                "       auctions.bytes,"
                "       extra_value + p.latest_price AS total_value,"
                "       auctions.price - (extra_value + p.latest_price) AS profit "
                "FROM auctions "
                "  LEFT JOIN (SELECT LatestPrice(item_name) AS latest_price, auctions.id FROM auctions)"
                "  p ON p.id = auctions.id "
                "WHERE (auctions.price < %s OR 1=%s) AND extra_value + p.latest_price + %s > auctions.price",
                (max_money, max_money is None, min_profit))  # todo bins
            return {'last_update': app.config['grabber'].last_update, 'auctions': await cur.fetchall()}


@app.route("/price/<item>")  # todo make into template
async def get_price(item):
    async with db.acquire() as conn:
        async with conn.cursor() as cur:
            return {'item': item,
                    'price': await cur.execute("SELECT LatestPrice(%s)", (item,))[0],
                    'last_update': app.config['grabber'].last_update}  # todo handle alias


@app.route("/prices/<item>")
async def get_price(item):
    async with db.acquire() as conn:
        async with conn.cursor(DictCursor) as cur:
            return {'item': item,
                    'prices': await cur.execute("SELECT price, time_checked FROM price WHERE item_name=%s", (item,))[0]}


if __name__ == "__main__":
    thread = AuctionGrabber(conf, loop)
    thread.start()
    app.config['grabber'] = thread
    app.run(host="127.0.0.1", port=5000, loop=loop)
