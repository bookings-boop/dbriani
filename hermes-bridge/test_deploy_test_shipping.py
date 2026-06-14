import re, pathlib
SRC = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "deploy_bridge.py"
src = SRC.read_text()

def test_test_modules_glob_present():
    assert 'BRIDGE_DIR.glob("test_*.py")' in src, "deploy must collect test_*.py for upload"

def test_test_modules_uploaded():
    assert "for tmod in test_modules:" in src and "upload-{tmod.name}" in src, \
        "deploy must upload each test_module"

def test_blocking_gate_excludes_tests():
    # the blocking COMPILE_OK gate must compile mod_paths (runtime only), and
    # mod_paths must be built from `modules`, never test_modules.
    assert "mod_paths = " in src and "for m in modules" in src
    block = src[src.index("mod_paths = "):src.index("COMPILE_OK")]
    assert "test_modules" not in block, "tests must NOT be in the blocking compile-gate"

def test_test_compile_is_warn_only():
    # the test compile must NOT call die(); it prints WARN instead.
    seg = src[src.index("compile-tests"):src.index("compile-tests")+600]
    assert "die(" not in seg, "shipped-test compile must be WARN-only, never die()"
    assert "WARN" in seg

for fn in [test_test_modules_glob_present, test_test_modules_uploaded,
           test_blocking_gate_excludes_tests, test_test_compile_is_warn_only]:
    fn(); print(f"PASS {fn.__name__}")
print("ALL PASS")
