import asyncio
import base64
import datetime
import io
import json
import pathlib
import sys
import time
from collections import defaultdict

import aiohttp
import discord
import mysql.connector
import nbt.nbt as nbt
import requests
import toml
from discord.ext import commands

from constants import *

songs = defaultdict(lambda: [])


class ConfigError(Exception):
    def __init__(self, message):
        print("*----- Please configure the bot in 'config.toml' before running -----*")
        print(message)


class AuctionGrabber:
    with open("alias.json", "r") as alias_file:
        aliases = json.loads(alias_file.read())

    def __init__(self, config):
        self.config = config
        self.key = self.config['api']['hypixel']
        self.min_price = defaultdict(lambda: 0)
        self.auctions = []
        self.prices = defaultdict(lambda: [])
        self.run_counter = 5
        self.last_update = 0

    async def get_pages(self, max_page, session):
        tasks = [self.get_page(i, session) for i in range(max_page + 1)]
        auc = [a for nested in await asyncio.gather(*tasks) for a in nested]
        if self.run_counter == 5:
            self.min_price['RECOMBOBULATOR_3000'] = \
                json.loads(await (await session.get(BAZAAR_ENDPOINT.format(self.key))).text())['products'][
                    'RECOMBOBULATOR_3000'][
                    'sell_summary'][0]['pricePerUnit']
            for item, i_price in self.prices.items():
                i_price = sorted(i_price)[:5]
                self.min_price[item] = i_price[len(i_price) // 2 - (0 if len(i_price) % 2 == 1 else 1)]
        return auc

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
        auctions = json.loads(await (await session.get(AUCTION_ENDPOINT.format(self.key, page))).text())['auctions']
        if self.run_counter == 5:
            page_prices = defaultdict(lambda: [])
            for auction in auctions:
                if 'bin' not in auction or auction['bin'] is False or auction['end'] < time.time() * 1000:
                    continue
                nbt_data, _, count = self.decode_item(auction['item_bytes'])

                # todo images (from texture pack or head)
                # if name not in self.images and 'SkullOwner' in nbt_data['tag'].keys():
                #     # Item is a head
                #     base64_texture = nbt_data['tag']['SkullOwner']['Properties']['textures'][0]['Value'].value
                #     self.images[name] = json.loads(str(base64.b64decode(base64_texture).decode()))['SKIN']['url']

                name = AuctionGrabber.get_name(nbt_data)
                page_prices[name].append(auction['starting_bid'] / count)
            for k, v in page_prices.items():
                self.prices[k].append(min(v))
        return [auc for auc in auctions if
                auc['start'] > self.last_update and (time.time() + 30) * 1000 < auc['end'] < (
                        time.time() + self.config['options']['min-time']) * 1000]

    async def check_flip(self) -> list:
        flips = []
        for item in self.auctions:
            price = item['starting_bid'] if item['starting_bid'] > item['highest_bid_amount'] else item[
                'highest_bid_amount']
            nbt_data, _, count = AuctionGrabber.decode_item(item['item_bytes'])
            name = AuctionGrabber.get_name(nbt_data)
            if price < self.min_price[name] - self.config['options']['min-profit']:
                flips.append(item)
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
    # todo Deal with pet rarity + xp, enchants and HPB
    # todo Handle aliases better
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

            db_connection = mysql.connector.connect(host=self.config['db']['host'],
                                                    user=self.config['db']['username'],
                                                    password=self.config['db']['password'],
                                                    database=self.config['db']['database'])

            if not db_connection.is_connected():
                raise ConfigError("MySQL credentials are incorrect")

            hypixel = self.config['api']['hypixel']
            if json.loads(requests.get(TOKEN_TEST.format(hypixel)).content)['cause'] == "Invalid API key":
                raise ConfigError("Invalid Hypixel API Key")

            self.grabber = AuctionGrabber(self.config)
            self.discord = self.config['api']['discord']
            self.db = db_connection

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
                guild: discord.Guild
                for guild in self.guilds:
                    await guild.text_channels[0].send(AuctionGrabber.format_uuid(auc["uuid"]))
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
