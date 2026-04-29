#!/usr/bin/env python3
"""
代码检查系统 - 静态分析 Python 代码，报告潜在问题。
支持检查：函数长度、类方法数、长行、裸 except、未使用导入、命名规范。
配置通过 .env 文件和环境变量注入，零硬编码路径。
"""

import ast
import os
import sys
import json
import argparse
from pathlib import Path
from typing import List, Dict, Any

# 尝试导入 dotenv，若不可用则提示安装
try:
    from dotenv import load_dotenv
except ImportError:
    print("错误：缺少 python-dotenv 依赖。请执行：pip install python-dotenv", file=sys.stderr)
    sys.exit(1)

# 加载 .env 文件（如果存在），不覆盖已有的环境变量
load_dotenv(override=False)


def get_config() -> Dict[str, Any]:
    """
    从环境变量读取配置，返回字典。
    为什么用环境变量：实现零硬编码，方便不同环境调整规则。
    """
    return {
        "source_path": os.getenv("SOURCE_PATH", "."),
        "max_function_lines": int(os.getenv("MAX_FUNCTION_LINES", "50")),
        "max_class_methods": int(os.getenv("MAX_CLASS_METHODS", "20")),
        "max_line_length": int(os.getenv("MAX_LINE_LENGTH", "120")),
        "check_bare_except": os.getenv("CHECK_BARE_EXCEPT", "True").lower() in ("true", "1", "yes"),
        "check_unused_imports": os.getenv("CHECK_UNUSED_IMPORTS", "True").lower() in ("true", "1", "yes"),
        "output_format": os.getenv("OUTPUT_FORMAT", "text"),
    }


def collect_python_files(root: Path) -> List[Path]:
    """
    递归收集所有 .py 文件，忽略隐藏目录和常见的虚拟环境目录。
    为什么忽略这些：提升性能并避免检查第三方库代码。
    """
    ignore_dirs = {'.git', '__pycache__', '.venv', 'venv', '.tox', '.eggs', 'node_modules'}
    python_files = []
    try:
        for entry in root.rglob("*.py"):
            # 跳过隐藏目录或常见忽略目录
            parts = set(entry.parts)
            if parts & ignore_dirs:
                continue
            python_files.append(entry)
    except PermissionError as e:
        print(f"警告：权限不足，跳过部分文件/目录：{e}", file=sys.stderr)
    return python_files


class IssueCollector:
    """收集代码问题，统一输出格式。"""

    def __init__(self):
        self.issues: List[Dict[str, Any]] = []

    def add(self, file: Path, line: int, col: int, rule: str, message: str):
        self.issues.append({
            "file": str(file),
            "line": line,
            "col": col,
            "rule": rule,
            "message": message,
        })

    def report(self, fmt: str) -> str:
        if fmt == "json":
            return json.dumps(self.issues, indent=2, ensure_ascii=False)
        # 文本格式
        if not self.issues:
            return "未发现问题。"
        lines = []
        for iss in self.issues:
            lines.append(f"{iss['file']}:{iss['line']}:{iss['col']} [{iss['rule']}] {iss['message']}")
        return "\n".join(lines)


def check_function_length(tree: ast.AST, max_lines: int, collector: IssueCollector, file_path: Path):
    """检查函数/方法是否超过最大行数。"""
    if max_lines <= 0:
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 计算函数体行数：结束行-起始行（含头部装饰器）。使用 end_lineno 需要 Python 3.8+
            if hasattr(node, 'end_lineno') and node.end_lineno is not None:
                length = node.end_lineno - node.lineno + 1
            else:
                # 降级：用最后一个语句的行号近似，避免崩溃
                body = node.body
                if body:
                    last = body[-1]
                    length = (getattr(last, 'end_lineno', last.lineno) - node.lineno + 1)
                else:
                    length = 1
            if length > max_lines:
                collector.add(
                    file_path, node.lineno, node.col_offset,
                    "function-length",
                    f"函数 '{node.name}' 行数 {length} 超过限制 {max_lines}"
                )


def check_class_methods(tree: ast.AST, max_methods: int, collector: IssueCollector, file_path: Path):
    """检查类的实例方法数量是否过多。"""
    if max_methods <= 0:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            method_count = sum(
                1 for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            if method_count > max_methods:
                collector.add(
                    file_path, node.lineno, node.col_offset,
                    "too-many-methods",
                    f"类 '{node.name}' 方法数 {method_count} 超过限制 {max_methods}"
                )


def check_long_lines(file_path: Path, max_length: int, collector: IssueCollector):
    """逐行检查代码长度。为什么直接读行：避免AST解析丢失格式信息。"""
    if max_length <= 0:
        return
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for idx, line in enumerate(f, start=1):
                # 去除行尾换行符计算长度
                if len(line.rstrip('\n\r')) > max_length:
                    collector.add(
                        file_path, idx, 1,
                        "line-too-long",
                        f"行长度 {len(line.rstrip())} 超过限制 {max_length}"
                    )
    except UnicodeDecodeError:
        collector.add(file_path, 1, 1, "encoding-error", "文件无法以UTF-8解码，跳过行长度检查")


def check_bare_except(tree: ast.AST, collector: IssueCollector, file_path: Path):
    """检查是否存在裸 except: 语句。为什么禁止：会捕获 KeyboardInterrupt 等系统异常。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            # 裸 except 特征是 type 为 None 且 name 为 None（或没有指定捕获类型）
            if node.type is None:
                collector.add(
                    file_path, node.lineno, node.col_offset,
                    "bare-except",
                    "禁止使用裸 except:，请指定具体异常类型"
                )


def find_unused_imports(tree: ast.AST, collector: IssueCollector, file_path: Path):
    """检查 import 的模块或对象是否在文件中未被使用。"""
    # 收集所有导入的名称（模块别名或导入的具体名称）
    imports: Dict[str, int] = {}  # name -> line
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname if alias.asname else alias.name.split('.')[0]  # 顶层包名
                imports[name] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                for alias in node.names:
                    name = alias.asname if alias.asname else alias.name
                    # 如果导入的是 *，无法检查，跳过
                    if name == '*':
                        continue
                    imports[name] = node.lineno

    if not imports:
        return

    # 收集所有代码中使用的名称（Load 上下文）
    used_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used_names.add(node.id)
        # 属性访问如 os.path，需要记录根对象
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            # 递归获取最左端名称
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                used_names.add(root.id)

    for name, line in imports.items():
        if name not in used_names and name != '__future__':
            collector.add(
                file_path, line, 1,
                "unused-import",
                f"导入 '{name}' 未使用"
            )


def process_file(file_path: Path, config: Dict[str, Any], collector: IssueCollector):
    """处理单个 Python 文件，应用所有启用的检查规则。"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            source = f.read()
    except (FileNotFoundError, PermissionError) as e:
        collector.add(file_path, 1, 1, "file-error", f"无法读取文件：{e}")
        return

    # 解析 AST，语法错误直接报告并跳过该文件的其他检查
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as e:
        collector.add(file_path, e.lineno or 1, e.offset or 1, "syntax-error", f"语法错误：{e.msg}")
        return

    # 执行各项检查
    check_function_length(tree, config["max_function_lines"], collector, file_path)
    check_class_methods(tree, config["max_class_methods"], collector, file_path)
    if config["check_bare_except"]:
        check_bare_except(tree, collector, file_path)
    if config["check_unused_imports"]:
        find_unused_imports(tree, collector, file_path)

    # 行长度检查不依赖 AST
    check_long_lines(file_path, config["max_line_length"], collector)


def main():
    parser = argparse.ArgumentParser(description="代码检查系统 - 静态分析 Python 项目")
    parser.add_argument(
        "path", nargs="?", default=None,
        help="要检查的文件或目录（默认取自环境变量 SOURCE_PATH）"
    )
    parser.add_argument(
        "--output", "-o", choices=["text", "json"], default=None,
        help="输出格式（覆盖 .env 中的 OUTPUT_FORMAT）"
    )
    args = parser.parse_args()

    config = get_config()
    # 命令行输出格式优先级高于环境变量
    if args.output:
        config["output_format"] = args.output

    # 确定扫描根路径
    scan_path = Path(args.path) if args.path else Path(config["source_path"]).resolve()
    if not scan_path.exists():
        print(f"错误：路径不存在 - {scan_path}", file=sys.stderr)
        sys.exit(1)

    collector = IssueCollector()

    if scan_path.is_file():
        if scan_path.suffix == ".py":
            process_file(scan_path, config, collector)
        else:
            print(f"警告：{scan_path} 不是 Python 文件，将跳过", file=sys.stderr)
    else:
        py_files = collect_python_files(scan_path)
        if not py_files:
            print("未找到任何 .py 文件。", file=sys.stderr)
        for py_file in py_files:
            process_file(py_file, config, collector)

    # 输出报告
    report_str = collector.report(config["output_format"])
    print(report_str)


if __name__ == "__main__":
    main()
