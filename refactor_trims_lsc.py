#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Improved Class-Aware Refactoring Script for TRIMS LSC Module
Properly extracts complete classes with correct indentation

Usage:
    python refactor_trims_lsc_v2.py --source /path/to/trims_lsc_details_gui.py --dry-run
    python refactor_trims_lsc_v2.py --source /path/to/trims_lsc_details_gui.py --execute
"""
from __future__ import annotations

import os
import re
import sys
import shutil
import argparse
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Dict, Optional

# Fix Windows console encoding
if sys.platform == 'win32':
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')


class ClassExtractor:
    """Extract complete class definitions from source file"""
    
    def __init__(self, source_lines: List[str]):
        self.lines = source_lines
        self.class_map = self._build_class_map()
    
    def _build_class_map(self) -> Dict[str, Tuple[int, int]]:
        """Build map of class names to their line ranges"""
        class_map = {}
        
        for i, line in enumerate(self.lines, 1):
            if re.match(r'^class\s+(\w+)', line):
                match = re.match(r'^class\s+(\w+)', line)
                class_name = match.group(1)
                start = i
                end = self._find_class_end(start)
                class_mapclass_name = (start, end)
        
        return class_map
    
    def _find_class_end(self, start_line: int) -> int:
        """Find where a class definition ends"""
        # Classes end when we hit another top-level class or function, or EOF
        for i in range(start_line, len(self.lines)):
            line = self.linesi
            # Check for next top-level class/function (no indentation)
            if i > start_line and line and not line[0].isspace():
                if re.match(r'^(class|def)\s+', line):
                    return i
        return len(self.lines)
    
    def extract_class(self, class_name: str) -> Optional[List[str]]:
        """Extract complete class definition"""
        if class_name not in self.class_map:
            print(f"    WARN Class '{class_name}' not found in source")
            return []
        
        start, end = self.class_mapclass_name
        return self.lines[start-1:end]
    
    def extract_classes(self, *class_names: str) -> List[str]:
        """Extract multiple classes"""
        result = []
        for name in class_names:
            lines = self.extract_class(name)
            if lines:
                result.extend(lines)
                result.append('\n')  # Blank line between classes
            else:
                print(f"    WARN Skipping missing class: {name}")
        return result


class TRIMSLSCRefactorerV2:
    """Improved refactoring tool with proper class extraction"""
    
    # Define class groupings
    PARSER_CLASSES = {
        'quantulus': ['QuantulusParserStrategy', 'QuantulusRegistryParserModern', '_QWin'],
        'hidex': ['HidexMatrixParserStrategy'],
        'aloka': ['AlokaCsvParserStrategy'],
        'packard': ['PackardLSCParser'],
        'generic': ['DelimitedParserStrategy', 'HidexLSCParser'],
    }
    
    WORKER_CLASSES = ['LSCImportWorker']
    DIALOG_CLASSES = ['HidexMatrixMappingDialog', 'ColumnMappingDialog']
    MAIN_DIALOG = ['TrimsLSCImportDialog', 'TrimsLSCDetailsWindow']
    UTILITY_CLASSES = ['LSCWindow']
    
    def __init__(self, source_file: str, output_dir: str = None):
        self.source_file = Path(source_file)
        self.output_dir = Path(output_dir) if output_dir else self.source_file.parent / 'trims_lsc'
        self.backup_dir = self.source_file.parent / f'backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
        
        # Read source
        with open(self.source_file, 'r', encoding='utf-8', errors='replace') as f:
            self.source_lines = f.readlines()
        
        self.extractor = ClassExtractor(self.source_lines)
        self.moved_items = []
        
        # Show what classes were found
        print(f"\n[*] Found {len(self.extractor.class_map)} classes in source file:")
        for class_name, (start, end) in sorted(self.extractor.class_map.items(), key=lambda x: x[10]):
            lines = end - start
            print(f"    {class_name:<40} lines {start:4}-{end:4} ({lines} lines)")
    
    def create_backup(self):
        """Create backup of original files"""
        print(f"[*] Creating backup in {self.backup_dir}")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        
        shutil.copy2(self.source_file, self.backup_dir / self.source_file.name)
        
        # Backup protocol files
        for fname in ['lsc_protocol_manager.py', 'lsc_protocol_gui.py']:
            fpath = self.source_file.parent / fname
            if fpath.exists():
                shutil.copy2(fpath, self.backup_dir / fname)
        
        print(f"OK Backup created")
    
    def create_package_structure(self):
        """Create package directories"""
        print(f"[*] Creating package structure in {self.output_dir}")
        
        dirs = [
            self.output_dir,
            self.output_dir / 'parsers',
            self.output_dir / 'protocols',
            self.output_dir / 'computation',
            self.output_dir / 'workers',
            self.output_dir / 'gui',
            self.output_dir / 'utils',
        ]
        
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
            print(f"  OK {d.relative_to(self.source_file.parent)}")
    
    def create_file(self, filepath: Path, imports: str, content: List[str], description: str):
        """Create a file with header, imports, and content"""
        if not content:
            print(f"    WARN No content to write for {filepath.name}, skipping")
            return
        
        header = f'''"""
{description}

Extracted from trims_lsc_details_gui.py
Date: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
"""

'''
        
        with open(filepath, 'w', encoding='utf-8', errors='replace') as f:
            f.write(header)
            f.write(imports)
            f.write('\n\n')
            f.writelines(content)
        
        print(f"  OK Created {filepath.name}")
    
    def extract_base_parser(self):
        """Extract LSCParserStrategy base class"""
        base_lines = self.extractor.extract_class('LSCParserStrategy')
        
        imports = '''from abc import ABC, abstractmethod
import pandas as pd
from typing import Optional, List, Dict
'''
        
        self.create_file(
            self.output_dir / 'parsers' / 'base.py',
            imports,
            base_lines,
            'LSC Parser Base Class'
        )
        self.moved_items.append(('LSCParserStrategy', 'parsers/base.py'))
    
    def extract_quantulus_parsers(self):
        """Extract Quantulus parser classes"""
        classes = self.extractor.extract_classes(
            'QuantulusParserStrategy',
            '_QWin',
            'QuantulusRegistryParserModern'
        )
        
        imports = '''import re
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional, List, Dict, Iterator
from .base import LSCParserStrategy
'''
        
        self.create_file(
            self.output_dir / 'parsers' / 'quantulus.py',
            imports,
            classes,
            'Quantulus LSC Parser Strategies'
        )
        self.moved_items.append(('Quantulus parsers', 'parsers/quantulus.py'))
    
    def extract_hidex_parsers(self):
        """Extract HIDEX parser classes"""
        classes = self.extractor.extract_classes('HidexMatrixParserStrategy')
        
        imports = '''import pandas as pd
import numpy as np
import re
from typing import Optional, Dict, List
from .base import LSCParserStrategy
'''
        
        self.create_file(
            self.output_dir / 'parsers' / 'hidex.py',
            imports,
            classes,
            'HIDEX Matrix LSC Parser'
        )
        self.moved_items.append(('HIDEX parsers', 'parsers/hidex.py'))
    
    def extract_aloka_parser(self):
        """Extract Aloka parser"""
        classes = self.extractor.extract_classes('AlokaCsvParserStrategy')
        
        imports = '''import pandas as pd
from typing import Optional, Dict, List
from .base import LSCParserStrategy
'''
        
        self.create_file(
            self.output_dir / 'parsers' / 'aloka.py',
            imports,
            classes,
            'Aloka LSC Parser'
        )
        self.moved_items.append(('Aloka parser', 'parsers/aloka.py'))
    
    def extract_other_parsers(self):
        """Extract Packard and other parsers"""
        classes = self.extractor.extract_classes(
            'DelimitedParserStrategy',
            'PackardLSCParser',
            'HidexLSCParser'
        )
        
        imports = '''import pandas as pd
import re
from typing import Optional, Dict, List
from .base import LSCParserStrategy
'''
        
        self.create_file(
            self.output_dir / 'parsers' / 'other.py',
            imports,
            classes,
            'Other LSC Parsers (Packard, Generic)'
        )
        self.moved_items.append(('Other parsers', 'parsers/other.py'))
    
    def create_parser_factory(self):
        """Create parser factory"""
        factory_content = '''from typing import Optional
from .quantulus import QuantulusRegistryParserModern, QuantulusParserStrategy
from .hidex import HidexMatrixParserStrategy
from .aloka import AlokaCsvParserStrategy
from .other import PackardLSCParser, HidexLSCParser, DelimitedParserStrategy
from .base import LSCParserStrategy


def make_lsc_parser(format_id: int, format_name: str, filepath: str) -> LSCParserStrategy:
    """
    Factory function to create appropriate LSC parser.
    
    Args:
        format_id: Database format ID
        format_name: Format name
        filepath: Path to data file
    
    Returns:
        Parser instance
    """
    format_name_lower = (format_name or "").lower()
    
    # Quantulus
    if format_id in (1, 2) or "quantulus" in format_name_lower:
        if "registry" in format_name_lower or format_id == 1:
            return QuantulusRegistryParserModern(filepath)
        else:
            return QuantulusParserStrategy(filepath)
    
    # HIDEX Matrix
    elif format_id == 6:
        return HidexMatrixParserStrategy(filepath)
    
    # Aloka
    elif format_id == 5 or "aloka" in format_name_lower:
        return AlokaCsvParserStrategy(filepath)
    
    # Packard
    elif format_id == 3 or "packard" in format_name_lower:
        return PackardLSCParser(filepath)
    
    # HIDEX List or generic delimited
    elif format_id == 12:
        return HidexLSCParser(filepath)
    
    else:
        return DelimitedParserStrategy(filepath)
'''
        
        with open(self.output_dir / 'parsers' / 'factory.py', 'w', encoding='utf-8') as f:
            f.write('"""\nLSC Parser Factory\n"""\n\n')
            f.write(factory_content)
        
        print(f"  OK Created factory.py")
    
    def create_parsers_init(self):
        """Create parsers __init__.py"""
        init_content = '''"""LSC File Parsers"""

from .base import LSCParserStrategy
from .quantulus import QuantulusRegistryParserModern, QuantulusParserStrategy
from .hidex import HidexMatrixParserStrategy
from .aloka import AlokaCsvParserStrategy
from .other import PackardLSCParser, HidexLSCParser, DelimitedParserStrategy
from .factory import make_lsc_parser

__all__ = [
    'LSCParserStrategy',
    'make_lsc_parser',
    'QuantulusRegistryParserModern',
    'QuantulusParserStrategy',
    'HidexMatrixParserStrategy',
    'AlokaCsvParserStrategy',
    'PackardLSCParser',
    'HidexLSCParser',
    'DelimitedParserStrategy',
]
'''
        
        with open(self.output_dir / 'parsers' / '__init__.py', 'w', encoding='utf-8') as f:
            f.write(init_content)
    
    def phase1_extract_parsers(self):
        """Phase 1: Extract all parsers"""
        print("\n[*] Phase 1: Extracting Parsers")
        
        self.extract_base_parser()
        self.extract_quantulus_parsers()
        self.extract_hidex_parsers()
        self.extract_aloka_parser()
        self.extract_other_parsers()
        self.create_parser_factory()
        self.create_parsers_init()
        
        print("OK Phase 1 Complete")
    
    def extract_computation_functions(self):
        """Extract standalone computation functions"""
        # Find functions between classes
        funcs = []
        in_function = False
        func_start = None
        
        for i, line in enumerate(self.source_lines, 1):
            # Top-level function
            if re.match(r'^def\s+', line):
                if not in_function:
                    in_function = True
                    func_start = i
            # Next top-level item
            elif line and not line[0].isspace() and in_function:
                if re.match(r'^(class|def)\s+', line):
                    funcs.extend(self.source_lines[func_start-1:i-1])
                    in_function = False
        
        return funcs
    
    def phase2_extract_computation(self):
        """Phase 2: Extract computation functions"""
        print("\n[*] Phase 2: Extracting Computation Functions")
        
        # Extract standalone functions (lines 1709-2180 approximately)
        comp_lines = self.source_lines[1708:2180]
        
        imports = '''import pandas as pd
import numpy as np
from typing import Tuple, Optional
from sqlalchemy import text
from db_core import db_manager
from shared_utils import get_standard_activity, get_global_value
from datetime import datetime
'''
        
        self.create_file(
            self.output_dir / 'computation' / 'statistics.py',
            imports,
            comp_lines,
            'Statistical Computation Functions'
        )
        
        # Create computation __init__
        comp_init = '''"""LSC Computation Functions"""

from .statistics import *

__all__ = [
    'compute_means',
    'compute_run_params',
    '_compute_net_activity_dpm_row',
]
'''
        with open(self.output_dir / 'computation' / '__init__.py', 'w', encoding='utf-8') as f:
            f.write(comp_init)
        
        self.moved_items.append(('Computation functions', 'computation/statistics.py'))
        print("OK Phase 2 Complete")
    
    def phase3_extract_worker(self):
        """Phase 3: Extract import worker"""
        print("\n[*] Phase 3: Extracting Import Worker")
        
        worker_lines = self.extractor.extract_class('LSCImportWorker')
        
        imports = '''import os
import logging
import pandas as pd
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal
from ..parsers.factory import make_lsc_parser
'''
        
        self.create_file(
            self.output_dir / 'workers' / 'import_worker.py',
            imports,
            worker_lines,
            'LSC Import Worker Thread'
        )
        
        # Create workers __init__
        worker_init = '''"""LSC Worker Threads"""

from .import_worker import LSCImportWorker

__all__ = ['LSCImportWorker']
'''
        with open(self.output_dir / 'workers' / '__init__.py', 'w', encoding='utf-8') as f:
            f.write(worker_init)
        
        self.moved_items.append(('LSCImportWorker', 'workers/import_worker.py'))
        print("OK Phase 3 Complete")
    
    def phase4_extract_main_dialog(self):
        """Phase 4: Extract main dialog"""
        print("\n[*] Phase 4: Extracting Main Dialog")
        
        dialog_lines = self.extractor.extract_class('TrimsLSCImportDialog')
        
        imports = '''import sys
import logging
import pandas as pd
import numpy as np
import os
import re
from typing import Optional, List, Dict, Tuple
from datetime import datetime

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from PyQt5.QtWidgets import *
from PyQt5.QtGui import *
from PyQt5.QtCore import *
from PyQt5.QtPrintSupport import QPrinter, QPrintDialog

from ..parsers import make_lsc_parser
from ..workers import LSCImportWorker
from ..computation import compute_means, compute_run_params, _compute_net_activity_dpm_row

from db_core import db_manager
from sqlalchemy import text
from shared_utils import *

try:
    from lsc_protocol_manager import LSCProtocolManager
    from lsc_protocol_gui import ProtocolEditorDialog, ProtocolManagerDialog
except ImportError:
    # Protocols not yet refactored
    LSCProtocolManager = None
    ProtocolEditorDialog = None
    ProtocolManagerDialog = None
'''
        
        self.create_file(
            self.output_dir / 'gui' / 'main_dialog.py',
            imports,
            dialog_lines,
            'TRIMS LSC Import Dialog - Main GUI'
        )
        
        # Create gui __init__
        gui_init = '''"""TRIMS LSC GUI Components"""

from .main_dialog import TrimsLSCImportDialog

__all__ = ['TrimsLSCImportDialog']
'''
        with open(self.output_dir / 'gui' / '__init__.py', 'w', encoding='utf-8') as f:
            f.write(gui_init)
        
        self.moved_items.append(('TrimsLSCImportDialog', 'gui/main_dialog.py'))
        print("OK Phase 4 Complete")
    
    def create_package_init(self):
        """Create main package __init__"""
        init_content = '''"""
TRIMS LSC Module - Liquid Scintillation Counting Analysis
Modular architecture for LSC data import and analysis
"""

__version__ = '2.0.0'

from .parsers import make_lsc_parser
from .workers import LSCImportWorker
from .gui import TrimsLSCImportDialog

__all__ = [
    'make_lsc_parser',
    'LSCImportWorker',
    'TrimsLSCImportDialog',
]
'''
        
        with open(self.output_dir / '__init__.py', 'w', encoding='utf-8') as f:
            f.write(init_content)
        
        print("OK Package __init__.py created")
    
    def create_compatibility_wrapper(self):
        """Create backward compatibility wrapper"""
        compat_content = f'''"""
TRIMS LSC Details GUI - Backward Compatibility Wrapper

Refactored on: {datetime.now().strftime("%Y-%m-%d")}
Backup: {self.backup_dir.name}
"""

from trims_lsc.parsers import *
from trims_lsc.workers import *
from trims_lsc.gui import *

try:
    from lsc_protocol_manager import *
    from lsc_protocol_gui import *
except ImportError:
    pass

__all__ = [
    'make_lsc_parser',
    'LSCImportWorker',
    'TrimsLSCImportDialog',
]
'''
        
        compat_file = self.source_file.parent / 'trims_lsc_details_gui_new.py'
        with open(compat_file, 'w', encoding='utf-8') as f:
            f.write(compat_content)
        
        print(f"OK Created {compat_file.name}")
    
    def generate_report(self):
        """Generate migration report"""
        report = f"""
TRIMS LSC REFACTORING REPORT (V2 - Class-Aware)
{'='*80}
Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

SOURCE: {self.source_file}
OUTPUT: {self.output_dir}
BACKUP: {self.backup_dir}

EXTRACTED COMPONENTS:
"""
        for item, dest in self.moved_items:
            report += f"\n  OK {item:<40} -> {dest}"
        
        report += f"""

NEXT STEPS:
1. Review files in: {self.output_dir}
2. Test: python -c "from trims_lsc import TrimsLSCImportDialog"
3. Run your application
4. If OK, rename trims_lsc_details_gui_new.py -> trims_lsc_details_gui.py
"""
        
        report_file = self.output_dir.parent / 'REFACTORING_REPORT_V2.txt'
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)
        
        print(f"\n[*] Report saved to: {report_file.name}")
    
    def run(self, dry_run=False):
        """Execute refactoring"""
        print("\n" + "="*80)
        print("TRIMS LSC AUTOMATED REFACTORING V2 (Class-Aware)")
        print("="*80)
        
        if dry_run:
            print("\nWARN DRY RUN MODE - No files modified\n")
        
        try:
            if not dry_run:
                self.create_backup()
                self.create_package_structure()
            else:
                print("[*] Would create backup and package structure")
            
            phases = [
                ("Phase 1: Parsers", self.phase1_extract_parsers),
                ("Phase 2: Computation", self.phase2_extract_computation),
                ("Phase 3: Worker", self.phase3_extract_worker),
                ("Phase 4: Main Dialog", self.phase4_extract_main_dialog),
            ]
            
            for phase_name, phase_func in phases:
                if not dry_run:
                    phase_func()
                else:
                    print(f"[*] Would execute: {phase_name}")
            
            if not dry_run:
                self.create_package_init()
                self.create_compatibility_wrapper()
                self.generate_report()
            
            print("\n" + "="*80)
            if dry_run:
                print("DRY RUN COMPLETE")
            else:
                print("REFACTORING COMPLETE!")
                print(f"Package: {self.output_dir}")
                print(f"Backup: {self.backup_dir}")
            print("="*80 + "\n")
            
            return True
            
        except Exception as e:
            print(f"\nERROR {e}")
            import traceback
            traceback.print_exc()
            return False


def main():
    parser = argparse.ArgumentParser(description='TRIMS LSC Refactoring Tool V2')
    parser.add_argument('--source', required=True, help='Path to trims_lsc_details_gui.py')
    parser.add_argument('--output', help='Output directory (default: ./trims_lsc)')
    parser.add_argument('--dry-run', action='store_true', help='Preview only')
    parser.add_argument('--execute', action='store_true', help='Execute refactoring')
    
    args = parser.parse_args()
    
    if not (args.dry_run or args.execute):
        print("Error: Use --dry-run or --execute")
        return 1
    
    if args.dry_run and args.execute:
        print("Error: Cannot use both flags")
        return 1
    
    refactorer = TRIMSLSCRefactorerV2(args.source, args.output)
    success = refactorer.run(dry_run=args.dry_run)
    
    return 0 if success else 1


if __name__ == '__main__':
    exit(main())