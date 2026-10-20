import jieba


def main():
    texts = {
        "问题": "为什么复数域不能成为与加法、乘法相容的有序域？",
        "原文片段": "复数域 C 不是一个有序数域",
    }

    for label, text in texts.items():
        print()
        print(label, "：", text)
        print("精确模式：", jieba.lcut(text))
        print("搜索模式：", jieba.lcut_for_search(text))


if __name__ == "__main__":
    main()