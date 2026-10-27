"""A definition written as an assignment (React wrappers, `module.exports.f = () => {}`, a TS arrow
with a `{…}` return type) or as an expression-bodied C#/Dart/Scala member is found, not "not found"."""

import pytest

from tools.block_extractor import extract_block, get_context_lines


# Each answered "" (not found — which Gate 1 reads as a hallucination) before EXTRACT-GAPS.
@pytest.mark.parametrize("ext,name,source,block", [
    # React wrappers
    (".jsx", "Button", "import React from 'react';\nconst Button = React.memo(function Button(props) {\n"
     "  return <b>{props.x}</b>;\n});\nexport default Button;\n",
     "const Button = React.memo(function Button(props) {\n  return <b>{props.x}</b>;\n});"),
    (".jsx", "Button", "const Button = React.memo((props) => {\n  return <b/>;\n});\n",
     "const Button = React.memo((props) => {\n  return <b/>;\n});"),
    (".tsx", "Input", "const Input = forwardRef<HTMLInputElement, Props>((props, ref) => {\n"
     "  return <input ref={ref} />;\n});\n",
     "const Input = forwardRef<HTMLInputElement, Props>((props, ref) => {\n  return <input ref={ref} />;\n});"),
    (".tsx", "Input", "export const Input = React.forwardRef((props, ref) => <input ref={ref} />);\n",
     "export const Input = React.forwardRef((props, ref) => <input ref={ref} />);"),
    (".js", "Wrapped", "const Wrapped = memo(Inner);\n", "const Wrapped = memo(Inner);"),
    (".js", "Connected", "const Connected = connect(mapState)(View);\n", "const Connected = connect(mapState)(View);"),
    (".js", "useThing", "export const useThing = useCallback(() => {\n  go();\n}, []);\n",
     "export const useThing = useCallback(() => {\n  go();\n}, []);"),
    # module.exports.f = arrow
    (".js", "f", "module.exports.f = () => {\n  return 1;\n};\n", "module.exports.f = () => {\n  return 1;\n}"),
    (".js", "g", "module.exports.g = async (a, b) => {\n  return a + b;\n};\n",
     "module.exports.g = async (a, b) => {\n  return a + b;\n}"),
    (".js", "h", "exports.h = (x) => x * 2;\nother();\n", "exports.h = (x) => x * 2;"),
    (".js", "k", "module.exports.k = x => x + 1;\n", "module.exports.k = x => x + 1;"),
    # TS arrows whose return type has braces
    (".ts", "mk", "const mk = (a: number): { x: number; y: string } => {\n  return { x: a, y: '' };\n};\n",
     "const mk = (a: number): { x: number; y: string } => {\n  return { x: a, y: '' };\n}"),
    (".ts", "mk", "export const mk = async (): Promise<{ ok: boolean }> => {\n  return { ok: true };\n};\n",
     "export const mk = async (): Promise<{ ok: boolean }> => {\n  return { ok: true };\n}"),
    (".ts", "mk", "const mk = (): {a: 1} => ({a: 1});\n", "const mk = (): {a: 1} => ({a: 1});"),
    (".ts", "fld", "class A {\n  fld = (a: number): { x: number } => {\n    return { x: a };\n  };\n}\n",
     "  fld = (a: number): { x: number } => {\n    return { x: a };\n  }"),
    # C# expression-bodied members
    (".cs", "Area", "class S {\n  public double Area() => w * h;\n  public int Other() { return 1; }\n}\n",
     "  public double Area() => w * h;"),
    (".cs", "Name", "class S {\n  public string Name => _name;\n  int Z() { return 0; }\n}\n",
     "  public string Name => _name;"),
    (".cs", "Twice", "class S {\n  public static int Twice(int x)\n      => x * 2;\n}\n",
     "  public static int Twice(int x)\n      => x * 2;"),
    # Dart
    (".dart", "area", "class S {\n  double area() => w * h;\n  int other() { return 1; }\n}\n",
     "  double area() => w * h;"),
    (".dart", "full", "class S {\n  String get full => '$a $b';\n}\n", "  String get full => '$a $b';"),
    (".dart", "load", "Future<int> load() async => 42;\n", "Future<int> load() async => 42;"),
    # Scala
    (".scala", "area", "object S {\n  def area(w: Int, h: Int): Int = w * h\n  def other(): Int = { 1 }\n}\n",
     "  def area(w: Int, h: Int): Int = w * h"),
    (".scala", "tw", "object S {\n  def tw(x: Int) =\n    x * 2\n  val z = 1\n}\n", "  def tw(x: Int) =\n    x * 2"),
])
def test_an_assigned_or_expression_bodied_definition_is_found(ext, name, source, block):
    assert extract_block(source, name, ext) == block
    # get_context_lines shares the search, so it stands on the same line
    above = source[:source.index(block.splitlines()[0])]
    assert get_context_lines(source, name, before=5, file_ext=ext) == "".join(
        above.splitlines(keepends=True)[-5:])


# A call, a reassignment, a lambda parameter or a type member is still no definition.
@pytest.mark.parametrize("ext,name,source", [
    (".js", "total", "const total = sum(items);\n"),
    (".js", "f", "function g() {\n  f = compute(1);\n}\n"),
    (".js", "x", "x = (a + b);\n"),
    (".js", "cb", "run(cb => cb + 1);\n"),
    (".ts", "onSave", "interface P {\n  onSave: (v: string) => void;\n}\n"),
    (".cs", "Name", "var q = items.Where(Name => Name > 1);\n"),
    (".cs", "x", "  return x => x + 1;\n"),
    (".cs", "Red", "var s = c switch {\n  Color.Red => \"r\",\n};\n"),
    (".dart", "go", "  onPressed: () => go(),\n"),
    (".scala", "f", "val y = f(2)\n"),
])
def test_a_use_of_the_name_is_still_not_a_definition(ext, name, source):
    assert extract_block(source, name, ext) == ""


def test_a_braced_definition_still_wins_over_an_assigned_one():
    source = "exports.f = () => 1;\nfunction f() {\n  return 2;\n}\n"
    assert extract_block(source, "f", ".js") == "function f() {\n  return 2;\n}"
