import os
import re

def process_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    original_content = content

    # 1. Single line: except Exception: pass
    content = re.sub(
        r'(\s*)except Exception:\s*pass',
        r'\1except Exception as e:\n\1    logging.warning(f"Exception caught: {e}")',
        content
    )

    # 2. Multi-line: except Exception:\n    pass
    # We capture the indentation of the except block and the pass block.
    content = re.sub(
        r'(\s*)except Exception:\n(\s+)pass',
        r'\1except Exception as e:\n\2logging.warning(f"Exception caught: {e}")',
        content
    )

    # 3. Single line: except Exception: continue
    content = re.sub(
        r'(\s*)except Exception:\s*continue',
        r'\1except Exception as e:\n\1    logging.warning(f"Exception caught: {e}"); continue',
        content
    )

    # 4. Multi-line: except Exception:\n    continue
    content = re.sub(
        r'(\s*)except Exception:\n(\s+)continue',
        r'\1except Exception as e:\n\2logging.warning(f"Exception caught: {e}")\n\2continue',
        content
    )
    
    # 5. Single line: except Exception: return <something>
    content = re.sub(
        r'(\s*)except Exception:\s*return(.*)',
        r'\1except Exception as e:\n\1    logging.warning(f"Exception caught: {e}"); return\2',
        content
    )

    # 6. Multi-line: except Exception:\n    return <something>
    content = re.sub(
        r'(\s*)except Exception:\n(\s+)return(.*)',
        r'\1except Exception as e:\n\2logging.warning(f"Exception caught: {e}")\n\2return\3',
        content
    )

    if content != original_content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        return True
    return False

modified_files = 0
for root, dirs, files in os.walk('.'):
    if '.venv' in root or '.git' in root or '__pycache__' in root:
        continue
    for file in files:
        if file.endswith('.py') and file != 'fix_excepts.py':
            filepath = os.path.join(root, file)
            if process_file(filepath):
                print(f"Fixed: {filepath}")
                modified_files += 1

print(f"Total files modified: {modified_files}")
