# Hypixel Auction Bot

Hypixel Auction bot is a python bot made to iterate over the hypixel auction house and return auctions that can be resold for a higher price

![Checking the price gif](https://files.jack-chapman.com/auctionbot.gif)

## Installation
The bot currently depends on discord.py as it is the only way to access the bot. Before trying to use the bot make sure to download the required dependencies and configure the bot in `config.toml`.

```bash
pip install -r requirements.txt
python3 bot.py  # This will create the config.toml file
                # which should be edited before running
python3 bot.py
```

## Roadmap
* Once the bot is fully functional, the bot will be made into a web api using [flask](https://github.com/pallets/flask).
* Web interface 
* Add images from FurfSky and minecraft texture packs
* Better alias system

## Contributing
Contributions are welcome, but the state of the bot is rapidly changing and 