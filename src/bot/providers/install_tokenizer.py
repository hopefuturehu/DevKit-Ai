"""Explicit tokenizer setup: python -m bot.providers.install_tokenizer."""

from bot.providers.token_counting import install_tokenizer

if __name__ == "__main__":
    print(install_tokenizer())
