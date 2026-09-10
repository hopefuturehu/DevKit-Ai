# DeepSeek V4 encoder

Unmodified `encoding/encoding_dsv4.py` from the official MIT-licensed model repository:
https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/tree/60d8d70770c6776ff598c94bb586a859a38244f1/encoding

SHA-256: `bdbd57c132a1b3725042323d02b98b9d1df28e5f388f134399555d041f5055e0`.
Only `encode_messages` is used, for local token estimation. No upstream output parser is used.
The tokenizer vocabulary is installed separately by `python -m bot.providers.install_tokenizer`.
