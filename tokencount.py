import os
import argparse
import tiktoken

# GPT tokenizer
encoding = tiktoken.get_encoding("cl100k_base")

# 忽略目录
IGNORE_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".idea",
    ".vscode",
    "logs"
}

# 需要统计的文件类型
VALID_EXT = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".js",
    ".ts",
    ".vue"
}


def count_tokens(text: str) -> int:
    return len(encoding.encode(text))


def analyze_file(file_path):

    try:

        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()

        tokens = count_tokens(text)
        lines = text.count("\n") + 1
        size = len(text)

        return {
            "file": file_path,
            "tokens": tokens,
            "lines": lines,
            "size": size
        }

    except Exception:
        return None


def scan_directory(path):

    results = []

    for root, dirs, files in os.walk(path):

        # 过滤目录
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]

        for file in files:

            ext = os.path.splitext(file)[1]

            if ext in VALID_EXT:

                file_path = os.path.join(root, file)

                data = analyze_file(file_path)

                if data:
                    results.append(data)

    return results


def print_results(results):

    results.sort(key=lambda x: x["tokens"], reverse=True)

    total_tokens = 0
    total_lines = 0
    total_files = len(results)

    print("\nFile Token Statistics\n")

    for r in results:

        print(
            f'{r["tokens"]:>8} tokens | {r["lines"]:>6} lines | {r["file"]}'
        )

        total_tokens += r["tokens"]
        total_lines += r["lines"]

    print("\nSummary")
    print("--------")
    print("Files :", total_files)
    print("Tokens:", total_tokens)
    print("Lines :", total_lines)


def main():

    parser = argparse.ArgumentParser(
        description="AI Project Token Analyzer"
    )

    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="file or directory"
    )

    args = parser.parse_args()

    path = args.path

    if os.path.isfile(path):

        result = analyze_file(path)

        if result:
            print(result)
        else:
            print("Cannot read file.")

        return

    results = scan_directory(path)

    if not results:
        print("No valid files found.")
        return

    print_results(results)


if __name__ == "__main__":
    main()








# python ai_token_analyzer.py trading_system

# python ai_token_analyzer.py strategy/trend_strategy.py

# python ai_token_analyzer.py --model llama