import os
import argparse

# 可统计行数的代码文件扩展名
CODE_EXTS = {'.py', '.ts', '.js', '.cpp', '.h', '.hpp', '.java', '.cs', '.go', '.rs', '.lua', '.json', '.md', '.txt'}

def count_lines(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in CODE_EXTS:
        return None
    
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            return sum(1 for _ in f)
    except:
        return None

def format_size(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"

def print_tree(path='.', prefix='', current_depth=0, max_depth=None):
    """
    :param path: 当前路径
    :param prefix: 打印前缀
    :param current_depth: 当前递归深度
    :param max_depth: 最大递归深度 (None 表示不限制)
    """
    # 如果设置了最大深度且当前深度已达到，则停止递归
    if max_depth is not None and current_depth > max_depth:
        return

    try:
        entries = sorted(os.listdir(path))
    except PermissionError:
        return

    for i, entry in enumerate(entries):
        full_path = os.path.join(path, entry)
        is_last = i == len(entries) - 1
        connector = '└── ' if is_last else '├── '
        
        line = prefix + connector + entry

        if os.path.isfile(full_path):
            size = os.path.getsize(full_path)
            lines = count_lines(full_path)
            size_str = format_size(size)
            if lines is not None:
                line += f"  [{size_str}, {lines} lines]"
            else:
                line += f"  [{size_str}]"
            print(line)

        elif os.path.isdir(full_path):
            print(line)
            # 下一层递归
            extension = '    ' if is_last else '│   '
            print_tree(full_path, prefix + extension, current_depth + 1, max_depth)

if __name__ == "__main__":
    # 配置命令行参数
    parser = argparse.ArgumentParser(description="目录树遍历工具 (带行数统计)")
    parser.add_argument("path", nargs="?", default=".", help="要遍历的目录路径 (默认当前目录)")
    parser.add_argument("-d", "--depth", type=int, default=None, help="最大递归深度 (例如 0 表示只列出当前层)")
    
    args = parser.parse_args()

    # 启动
    root_path = os.path.abspath(args.path)
    print(f"Directory: {root_path}")
    print_tree(root_path, max_depth=args.depth)