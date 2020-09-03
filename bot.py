import asyncio
import base64
import datetime
import io
import json
import pathlib
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import List
import aiohttp
import discord
import nbt.nbt as nbt
import requests
import toml
from discord.ext import commands
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


class AuctionGrabber:
    with open("alias.json", "r") as alias_file:
        aliases = json.loads(alias_file.read())

    def __init__(self, config):
        self.config = config
        self.key = self.config['api']['hypixel']
        self.min_price = defaultdict(lambda: 0)
        self.auctions = []
        self.notified = []
        self.prices = defaultdict(lambda: [])
        self.run_counter = 5
        self.last_update = 0

    async def get_pages(self, max_page, session):
        tasks = [self.get_page(i, session) for i in range(max_page + 1)]
        auc = [a for nested in await asyncio.gather(*tasks) for a in nested]
        if self.run_counter == 5:
            for price in await self.get_bazaar(session, "RECOMBOBULATOR_3000", "HOT_POTATO_BOOK", "FUMING_POTATO_BOOK"):
                self.min_price[price[0]] = price[1]
            for item, i_price in self.prices.items():
                # i_price = sorted(i_price)[:5]
                # self.min_price[item] = i_price[len(i_price) // 2 - (0 if len(i_price) % 2 == 1 else 1)]
                self.min_price[item] = min(i_price)

            # Set default prices for items that won't fluctuate as to not give fake values
            self.min_price["DRAGON_SLAYER"] = 1_000_000
        return auc

    async def get_bazaar(self, session, *items) -> list:
        async def get_bazaar_tuple(j, prod):
            return (prod, j['products'][prod][
                'sell_summary'][0]['pricePerUnit'])

        tasks = [get_bazaar_tuple(json.loads(await (await session.get(BAZAAR_ENDPOINT.format(self.key))).text()), prod)
                 for prod in items]
        return [p for p in await asyncio.gather(*tasks)]

    async def receive_auctions(self):
        async with aiohttp.ClientSession() as session:
            resp = await session.get(AUCTION_ENDPOINT.format(self.key, 0))
            page0 = json.loads(await resp.text())
            if page0['lastUpdated'] == self.last_update:
                return
            self.auctions = await self.get_pages(page0['totalPages'], session)
        self.last_update = page0['lastUpdated']
        self.run_counter = 0 if self.run_counter == 5 else self.run_counter + 1

    async def get_page(self, page: int, session: aiohttp.ClientSession) -> json:
        try:
            auctions_json = json.loads(await (await session.get(AUCTION_ENDPOINT.format(self.key, page))).text())
            if 'auctions' in auctions_json:
                auctions = auctions_json['auctions']
            else:
                return []
        except aiohttp.ServerDisconnectedError:
            return []
        if self.run_counter == 5:
            page_prices = defaultdict(lambda: [])
            for auction in auctions:
                if 'bin' not in auction or auction['bin'] is False or auction['end'] < time.time() * 1000:
                    continue
                nbt_data, _, count = self.decode_item(auction['item_bytes'])

                name = AuctionGrabber.get_name(nbt_data)
                page_prices[name].append(auction['starting_bid'] / count)
            for k, v in page_prices.items():
                self.prices[k].append(min(v))
        return [auc for auc in auctions if
                auc['uuid'] not in self.notified and (((time.time() + 30) * 1000 < auc['end'] < (
                        time.time() + self.config['options']['min-time']) * 1000) or ('bin' in auc and auc['bin']))]

    async def check_flip(self) -> List[Auction]:
        flips = []
        # j = json.loads(requests.get(PROFILE_ENDPOINT.format(self.key, "13283977ac354d92b950bc1fda73081d")).content)
        j = json.loads(requests.get(PROFILE_ENDPOINT.format(self.key, "ee4dfc679c4245c8a34e89cfa6c02d62")).content)

        # max_price = j['profile']['banking']['balance'] + j['profile']['members']['36e418428cce45ee878d82e2be986d49'][
        #     'coin_purse']
        # max_price = j['profile']['members']['36e418428cce45ee878d82e2be986d49'][
        #     'coin_purse']
        max_price = sum([int(j['profile']['members'][bal]['coin_purse']) for bal in j['profile']['members']])
        for item in self.auctions:
            price = item['starting_bid'] if item['starting_bid'] > item['highest_bid_amount'] else \
                item['highest_bid_amount'] * 1.15  # Factor in your bid
            if price > max_price:
                continue
            nbt_data, raw_name, count = AuctionGrabber.decode_item(item['item_bytes'])
            name = AuctionGrabber.get_name(nbt_data)

            # Modify price depending on other enchants
            extra_attributes = nbt_data['tag']['ExtraAttributes']
            value = 0
            if 'hot_potato_count' in extra_attributes:
                hpb_count = extra_attributes['hot_potato_count'].value
                if hpb_count > 10:
                    value += self.min_price['HOT_POTATO_BOOK'] * 10 + (hpb_count - 10) * self.min_price[
                        'FUMING_POTATO_BOOK']

            if self.config['options']['add-recombobulator'] and 'rarity_upgrades' in extra_attributes:
                value += self.min_price['RECOMBOBULATOR_3000']

            if 'wood_singularity_count' in extra_attributes and extra_attributes['wood_singularity_count'] == 1:
                value += self.min_price['WOOD_SINGULARITY']

            # Don't increase the value of enchants on dungeon items (can spawn with them)
            # or enchanted books since their enchant worth is already set
            if 'enchantments' in extra_attributes and not raw_name == "ENCHANTED_BOOK" and \
                    (('originTag' in extra_attributes and not extra_attributes['originTag'].value == "UNKNOWN")
                     and 'DUNGEON' not in item['item_lore']):
                for ench in extra_attributes['enchantments'].keys():
                    if ench in ENCHANTS and ENCHANTS[ench] <= extra_attributes['enchantments'][ench].value:
                        if ench == "dragon_hunter" or ench.startswith("ultimate"):
                            # base 2 multiplication
                            value += 2 ** (extra_attributes['enchantments'][ench].value - 1) \
                                     * self.min_price[ench.upper()]
                        else:
                            value += self.min_price[ench.upper()]

            image = 'https://sky.lea.moe/item/' + raw_name
            if 'SkullOwner' in nbt_data['tag']:
                head_json = json.loads(base64.b64decode(
                    nbt_data['tag']['SkullOwner']['Properties']['textures'][0]['Value'].value + "===").decode("utf-8"))
                image = 'https://sky.lea.moe/head/' + head_json['textures']['SKIN']['url'].split("texture/", 1)[1]

            auction_obj = Auction(id=AuctionGrabber.format_uuid(item['uuid']), name=name, price=price,
                                  extra_value=value, json_object=item, image=image,
                                  value=self.min_price[name] + value * PERCENTAGE_VALUE)
            price -= value * PERCENTAGE_VALUE  # Value of extras is decreased once added
            if price < self.min_price[name] - self.config['options']['min-profit']:
                flips.append(auction_obj)
                self.notified.append(auction_obj.json_object['uuid'])
            pass
        return flips

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


class AuctionBot(commands.AutoShardedBot):
    # todo Implement separate profit requirements between guilds
    # todo Deal with pet xp
    # todo Deal with fabled / reforge stones
    # todo Handle aliases better
    # todo Move check flip into page loading to save time waiting for all endpoints
    # todo Only show for player's money (web)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.loop.create_task(self.get_auctions())
        self.help_command = None

        path = pathlib.Path("config.toml")
        if not path.exists():
            with open("config.toml", "a+") as config_file:
                with open("defaultconfig.toml", "r") as default_config:
                    config_file.write(default_config.read())
            raise ConfigError("")
        else:
            with open("config.toml") as config_file:
                self.config = toml.loads(config_file.read())

            if not self.config['api']['hypixel'] or not self.config['api']['discord']:
                raise ConfigError("Ensure you have entered values for api keys")

            # db_connection = mysql.connector.connect(host=self.config['db']['host'],
            #                                         user=self.config['db']['username'],
            #                                         password=self.config['db']['password'],
            #                                         database=self.config['db']['database'])
            #
            # if not db_connection.is_connected():
            #     raise ConfigError("MySQL credentials are incorrect")
            # self.db = db_connection

            hypixel = self.config['api']['hypixel']
            if json.loads(requests.get(TOKEN_TEST.format(hypixel)).content)['cause'] == "Invalid API key":
                raise ConfigError("Invalid Hypixel API Key")

            self.grabber = AuctionGrabber(self.config)
            self.discord = self.config['api']['discord']

    def run(self):
        return super().run(self.discord)

    async def get_auctions(self):
        await self.wait_until_ready()
        ready = False
        while not self.is_closed():
            await self.grabber.receive_auctions()
            if not ready:
                print(f"Bot is now live on {len(self.guilds)} servers!")
            ready = True
            for auc in await self.grabber.check_flip():
                embed = discord.Embed(title=auc.json_object['item_name'],
                                      # description=re.sub(r"§[A-Fa-f0-9]", "", auc.json_object['item_lore']),
                                      colour=0x00FF00)
                embed.add_field(name="Current Price:", value="${:,d}".format(int(auc.price)), inline=True)
                embed.add_field(name="Estimated Value:", value="${:,d}".format(int(auc.value)), inline=True)
                embed.add_field(name="Projected Profit", value="${:,d}".format(int(auc.value - auc.price)),
                                inline=False)
                embed.add_field(name="Time Remaining", value="{}".format(auc.time_ending()), inline=False)
                embed.add_field(name="ID", value="{}".format(auc.id), inline=False)
                embed.set_footer(text="The value of things added decrease by {:d}% once added to an item.".format(
                    int(PERCENTAGE_VALUE * 100)))
                embed.set_thumbnail(url=auc.image)

                if self.config['options']['use-custom-protocol']:
                    # Custom Protocol: only currently works locally with registry entry + c++ application
                    embed.url = "https://skyblock.jack-chapman.com/" + auc.id

                await self.get_guild(749752189563437057).get_channel(749752189563437060).send(embed=embed)

            print("Updated! " + str(len(self.grabber.auctions)))
            await asyncio.sleep(60)


try:
    client = AuctionBot(command_prefix='a!')
except ConfigError:
    sys.exit()


@client.command(name="price", aliases=["p"])
async def price_command(ctx: commands.Context, *item):
    item_name = '_'.join(item).lower()
    if item_name in AuctionGrabber.aliases:
        item_name = AuctionGrabber.aliases[item_name]

    if item_name.upper() not in client.grabber.min_price:
        embed = discord.Embed(title="❌❌❌",
                              description="Item could not be found in the database!",
                              color=0xFF0000)
    else:
        price = client.grabber.min_price[item_name.upper()]
        friendly_name = item_name.replace('_', ' ').title()
        if item_name.lower() in ENCHANTS:
            friendly_name = friendly_name + str(ENCHANTS[item_name.lower()]) + " Book"
        embed = discord.Embed(title="💸 {} 💸".format(item_name.replace('_', ' ').title()),
                              description="The current price of {} is: $**{:,d}**".format(
                                  friendly_name,
                                  int(price)
                              ),
                              color=0x00FF00)
        if client.config['options']["wiki-link"]:
            async with aiohttp.ClientSession() as session:
                embed.url = \
                    json.loads(await (await session.get(WIKI_API.format(friendly_name))).text())['items'][0]['url']

    # todo Set image
    embed.set_footer(text="Last updated: ")
    embed.timestamp = datetime.datetime.utcfromtimestamp(client.grabber.last_update / 1000.0)
    await ctx.send(embed=embed)


def start_bot():
    client.run()


if __name__ == "__main__":
    start_bot()
