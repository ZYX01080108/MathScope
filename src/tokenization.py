import re

import jieba


def tokenize(text):
    tokens = []

    for raw_token in jieba.lcut_for_search(text):
        token = raw_token.strip().casefold()

        if token and re.search(r"\w", token):
            tokens.append(token)

    return tokens
def tokenize_normalized(text):
    normalized_text = text.replace("有序数域", "有序域")
    return tokenize(normalized_text)