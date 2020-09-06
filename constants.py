ENCHANTS = {
    'impaling': 3,
    'luck': 6,
    'ultimate_combo': 1,
    'ultimate_wise': 1,
    'ultimate_bank': 1,
    'ultimate_last_stand': 1,
    'ultimate_no_pain_no_gain': 1,
    'ultimate_rend': 1,
    'ultimate_jerry': 1,
    'ultimate_wisdom': 1,
    'dragon_slayer': 1,
    'critical': 6,
    'looting': 4,
    'ender_slayer': 6,
    'scavenger': 4,
    'vampirism': 6,
    'experience': 4,
    'life_steal': 4,
    'execute': 5,
    'giant_killer': 6,
    'sharpness': 6,
    'power': 6,
    'growth': 6,
    'protection': 6,
    'smite': 6,
    'bane_of_arthropods': 6,
    'angler': 6,
    'caster': 6,
    'frail': 6,
    'luck_of_the_sea': 6,
    'lure': 6,
    'magnet': 6,
    'spiked_hook': 6,
}

IGNORE = [
    'DRAGON_SLAYER',
]  # Items to not update price for

DEFAULT_CONFIG = """
[api]
hypixel = ""

[db]
host = ""
port = 3306
username = ""
password = ""
database = ""
"""

PERCENTAGE_VALUE = .85    # The cost multiplier of extra attributes once added to an item
AUCTION_ENDPOINT = r'https://api.hypixel.net/skyblock/auctions?key={}&page={}'
BAZAAR_ENDPOINT = r'https://api.hypixel.net/skyblock/bazaar?key={}'
PROFILE_ENDPOINT = r'https://api.hypixel.net/skyblock/profile?key={}&profile={}'
TOKEN_TEST = r'https://api.hypixel.net/token?key={}'
WIKI_API = "https://hypixel-skyblock.fandom.com/api/v1/Search/List?query={}&limit=1"
