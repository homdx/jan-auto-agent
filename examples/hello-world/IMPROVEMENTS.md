# IMPROVEMENTS.md
## Autonomous Tasks

### AUTO-T1: Add module-level docstring to main.py

**Cluster:** entry_orchestration  
**Location:** `main.py (lines 1–1)`  
**Target files:** `main.py`  
**Dependencies:** none  
**Acceptance check:**
```
python -c "import main; print(main.__doc__ is not None)"
```

**Instruction:**

Add a module-level docstring at the top of main.py that describes what the script does. The docstring should be a multi-line string (triple quotes) and should clearly state the purpose of the script.

---

### AUTO-T2: Add function-level docstring to main()

**Cluster:** entry_orchestration  
**Location:** `main.py → `main` (lines 1–1)`  
**Target files:** `main.py`  
**Dependencies:** none  
**Acceptance check:**
```
python -c "import main; print(main.main.__doc__ is not None)"
```

**Instruction:**

Add a function-level docstring to the `main()` function in main.py. The docstring should describe what the function does and its observable behavior.

---

### AUTO-T3: Add type hints to main() function

**Cluster:** entry_orchestration  
**Location:** `main.py → `main` (lines 1–1)`  
**Target files:** `main.py`  
**Dependencies:** none  
**Acceptance check:**
```
python -c "import main; import inspect; print(inspect.signature(main.main).return_annotation == type(None))"
```

**Instruction:**

Add type hints to the `main()` function signature in main.py. The function currently takes no arguments and returns no value, so use `-> None` for the return type.

---

### AUTO-T4: Add explicit sys.exit return code to main()

**Cluster:** entry_orchestration  
**Location:** `main.py → `main` (lines 1–5)`  
**Target files:** `main.py`  
**Dependencies:** none  
**Acceptance check:**
```
python main.py; echo $? | grep -q '^0$'
```

**Instruction:**

Modify the `main()` function in main.py to return an explicit exit code (0 for success) and update the `if __name__ == "__main__":` block to call `sys.exit(main())`. Ensure `import sys` is added at the top of the file.

---

### AUTO-T5: Add pytest test to verify script output

**Cluster:** entry_orchestration  
**Location:** `test_main.py`  
**Target files:** `test_main.py`  
**Dependencies:** `AUTO-T2`, `AUTO-T3`, `AUTO-T4`  
**Acceptance check:**
```
python -m pytest -q test_main.py
```

**Instruction:**

Create a new test file `test_main.py` (set `new_file: true`) with a pytest test that uses `capsys` to assert the script's printed output is exactly "Hello world\n". The test should invoke the `main()` function and check stdout.

---

### AUTO-T6: Create a pytest test file to assert printed output

**Cluster:** support  
**Location:** `test_main.py`  
**Target files:** `test_main.py`  
**Dependencies:** `AUTO-T2`, `AUTO-T3`, `AUTO-T4`  
**Acceptance check:**
```
python -m pytest -q test_main.py
```

**Instruction:**

Create a new file `test_main.py` in the same directory as main.py. Add a pytest test function that uses `capsys` to assert the script's printed output is exactly `Hello world\n`. The test should invoke the `main` function directly and check stdout.

---

## Manual Suggestions

_No manual suggestions._
