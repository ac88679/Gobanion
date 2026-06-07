import re, os

root = os.path.dirname(__file__)
for fn in os.listdir(root):
    if fn.startswith("test_") and fn.endswith(".py"):
        path = os.path.join(root, fn)
        with open(path, "r", encoding="utf-8") as f:
            c = f.read()
        old = c
        c = c.replace("\u2705", "[PASS]")
        if c != old:
            with open(path, "w", encoding="utf-8") as f:
                f.write(c)
            print(f"Fixed: {fn}")
