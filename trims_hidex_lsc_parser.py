"""
Hidex LSC Tritium Data Parser
Specialized parser for Hidex 300 SL LSC CSV files with tritium measurements.
"""
from __future__ import annotations

import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional, Union
from datetime import datetime


class HidexLSCParser:
    """Parser for Hidex LSC CSV data files."""
    
    def __init__(self, filepath: Union[str, Path], separator: str = ','):
        """
        Initialize the parser with a Hidex LSC CSV file.
        
        Args:
            filepath: Path to the Hidex LSC CSV file
            separator: Field separator (',' or ';'). Default: ',' 
                      Use 'auto' to auto-detect
        """
        self.filepath = Path(filepath)
        self.separator = separator
        self.data = None
        self.metadata = {}
        
    def _detect_separator(self) -> str:
        """
        Auto-detect the field separator (comma or semicolon).
        
        Returns:
            Detected separator (',' or ';')
        """
        with open(self.filepath, 'r', encoding='utf-8') as f:
            # Read first 20 lines to detect separator
            lines = [f.readline() for _ in range(20)]
            
            comma_count = sum(line.count(',') for line in lines)
            semicolon_count = sum(line.count(';') for line in lines)
            
            # Return the more common separator
            return ';' if semicolon_count > comma_count else ','
    
    def _find_header_line(self) -> int:
        """
        Find the line number containing the data header.
        
        Returns:
            Line number (0-indexed) of the header
        """
        # Key header columns to look for
        key_headers = ['Pos', 'SampleType', 'Time', 'CPM', 'EndTime', 'Rpt']
        
        with open(self.filepath, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f):
                line_upper = line.upper()
                # Check if this line contains at least 2 of our key headers
                matches = sum(1 for header in key_headers if header.upper() in line_upper)
                if matches >= 2:
                    return line_num
        
        # Default to line 1 (index 1) if not found
        return 1
    
    def parse(self) -> pd.DataFrame:
        """
        Parse the Hidex LSC CSV file.
        Automatically finds the header line based on key column names.
        
        Returns:
            DataFrame containing the measurement data
        """
        # Auto-detect separator if set to 'auto'
        if self.separator == 'auto':
            self.separator = self._detect_separator()
            self.metadata['separator_detected'] = self.separator
        
        # Find the header line automatically
        header_line = self._find_header_line()
        
        # Read CSV with the specified or detected delimiter
        self.data = pd.read_csv(
            self.filepath, 
            sep=self.separator, 
            header=header_line,
            encoding='utf-8'
        )
        
        # Convert EndTime to datetime
        if 'EndTime' in self.data.columns:
            self.data['EndTime'] = pd.to_datetime(self.data['EndTime'], errors='coerce')
        
        # Extract metadata from lines before header
        with open(self.filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            self.metadata['header_line'] = header_line + 1  # 1-indexed for user
            self.metadata['separator'] = self.separator
            
            # Store all metadata lines
            if header_line > 0:
                metadata_lines = []
                for i in range(header_line):
                    line = lines[i].strip()
                    if line:
                        metadata_lines.append(line)
                        # Try to parse key-value pairs
                        if ':' in line or '=' in line:
                            parts = line.split(':' if ':' in line else '=', 1)
                            if len(parts) == 2:
                                key = parts[0].strip().replace(' ', '_').lower()
                                value = parts[1].strip()
                                self.metadata[key] = value
                        else:
                            # Store first field of first line as sheet_type
                            if i == 0:
                                self.metadata['sheet_type'] = line.split(self.separator)[0]
                
                self.metadata['metadata_lines'] = metadata_lines
        
        # Add run information
        self.metadata['filename'] = self.filepath.name
        self.metadata['total_samples'] = len(self.data)
        self.metadata['positions'] = self.data['Pos'].unique().tolist()
        self.metadata['sample_types'] = self.data['SampleType'].unique().tolist()
        
        return self.data
    
    def get_sample_data(self, position: str = None, sample_type: str = None) -> pd.DataFrame:
        """
        Filter data by position and/or sample type.
        
        Args:
            position: Position identifier (e.g., 'A01', 'B03')
            sample_type: Sample type (e.g., 'Bkg', 'H3Std', 'Sample')
            
        Returns:
            Filtered DataFrame
        """
        if self.data is None:
            raise ValueError("No data loaded. Run parse() first.")
        
        filtered = self.data.copy()
        
        if position:
            filtered = filtered[filtered['Pos'] == position]
        
        if sample_type:
            filtered = filtered[filtered['SampleType'] == sample_type]
        
        return filtered
    
    def calculate_statistics(self, position: str = None, sample_type: str = None) -> pd.DataFrame:
        """
        Calculate statistics for CPM measurements.
        
        Args:
            position: Position to filter by (optional)
            sample_type: Sample type to filter by (optional)
            
        Returns:
            DataFrame with statistics for each position/sample type combination
        """
        if self.data is None:
            raise ValueError("No data loaded. Run parse() first.")
        
        filtered = self.get_sample_data(position, sample_type)
        
        # Group by position and sample type
        grouped = filtered.groupby(['Pos', 'SampleType'])
        
        # Calculate statistics for CPM columns
        cpm_columns = [col for col in filtered.columns if 'CPM' in col]
        
        stats_list = []
        
        for (pos, stype), group in grouped:
            stats_dict = {
                'Position': pos,
                'SampleType': stype,
                'n_measurements': len(group)
            }
            
            for col in cpm_columns:
                if col in group.columns:
                    stats_dict[f'{col}_mean'] = group[col].mean()
                    stats_dict[f'{col}_std'] = group[col].std()
                    stats_dict[f'{col}_min'] = group[col].min()
                    stats_dict[f'{col}_max'] = group[col].max()
                    stats_dict[f'{col}_median'] = group[col].median()

            # Also include QPE and QPI stats
            for col in ['QPE', 'QPI', 'TDCR3']:
                if col in group.columns:
                    stats_dict[f'{col}_mean'] = group[col].mean()
                    stats_dict[f'{col}_std'] = group[col].std()
            
            stats_list.append(stats_dict)
        
        return pd.DataFrame(stats_list)
    
    def calculate_background_corrected_cpm(self, 
                                          background_position: str = 'A01',
                                          cpm_column: str = 'CPMroi1') -> pd.DataFrame:
        """
        Calculate background-corrected CPM values.
        
        Args:
            background_position: Position containing background measurements
            cpm_column: CPM column to use for correction (default: 'CPMroi1')
            
        Returns:
            DataFrame with background-corrected values
        """
        if self.data is None:
            raise ValueError("No data loaded. Run parse() first.")
        
        # Get background data
        background_data = self.get_sample_data(position=background_position)
        background_mean = background_data[cpm_column].mean()

        # Create copy of data with corrected values
        corrected = self.data.copy()
        corrected[f'{cpm_column}_BkgCorrected'] = corrected[cpm_column] - background_mean
        corrected['Background_Mean'] = background_mean
        
        return corrected
    
    def calculate_dpm_and_activity(self, 
                                   efficiency: float = 0.35,
                                   sample_volume_ml: float = 1.0,
                                   cpm_column: str = 'CPMroi1') -> pd.DataFrame:
        """
        Calculate DPM and activity (Bq/L) from CPM measurements.
        
        Args:
            efficiency: Counting efficiency (0-1, default: 0.35 or 35%)
            sample_volume_ml: Sample volume in mL
            cpm_column: CPM column to use for calculations
            
        Returns:
            DataFrame with DPM and activity calculations
        """
        if self.data is None:
            raise ValueError("No data loaded. Run parse() first.")
        
        result = self.data.copy()
        
        # Calculate DPM (Disintegrations Per Minute)
        result['DPM'] = result[cpm_column] / efficiency
        
        # Calculate Bq (Becquerel = disintegrations per second)
        result['Bq'] = result['DPM'] / 60
        
        # Calculate activity concentration (Bq/L)
        sample_volume_l = sample_volume_ml / 1000
        result['Bq_per_L'] = result['Bq'] / sample_volume_l
        
        # Also in kBq/L for convenience
        result['kBq_per_L'] = result['Bq_per_L'] / 1000
        
        # Add efficiency used
        result['Efficiency_Used'] = efficiency
        
        return result
    
    def get_time_series(self, position: str, cpm_column: str = 'CPMroi1') -> pd.DataFrame:
        """
        Get time series data for a specific position.
        
        Args:
            position: Position identifier
            cpm_column: CPM column to plot
            
        Returns:
            DataFrame with time series data
        """
        if self.data is None:
            raise ValueError("No data loaded. Run parse() first.")
        
        data = self.get_sample_data(position=position)
        
        time_series = data[['EndTime', 'Rpt', cpm_column, 'QPE', 'TDCR3']].copy()
        time_series = time_series.sort_values('EndTime')
        
        return time_series
    
    def export_summary(self, output_path: Union[str, Path], 
                      include_statistics: bool = True,
                      include_calculations: bool = True,
                      efficiency: float = 0.35,
                      sample_volume_ml: float = 1.0):
        """
        Export comprehensive summary to Excel file.
        
        Args:
            output_path: Path for output Excel file
            include_statistics: Include statistics sheet
            include_calculations: Include DPM/activity calculations
            efficiency: Counting efficiency for calculations
            sample_volume_ml: Sample volume for calculations
        """
        if self.data is None:
            raise ValueError("No data loaded. Run parse() first.")
        
        output_path = Path(output_path)
        
        with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
            # Raw data
            self.data.to_excel(writer, sheet_name='Raw Data', index=False)
            
            # Statistics
            if include_statistics:
                stats = self.calculate_statistics()
                stats.to_excel(writer, sheet_name='Statistics', index=False)
            
            # Calculations
            if include_calculations:
                calc_data = self.calculate_dpm_and_activity(
                    efficiency=efficiency,
                    sample_volume_ml=sample_volume_ml
                )
                calc_data.to_excel(writer, sheet_name='DPM_Activity', index=False)
            
            # Metadata
            metadata_df = pd.DataFrame([self.metadata])
            metadata_df.to_excel(writer, sheet_name='Metadata', index=False)
        
        print(f"Summary exported to {output_path}")
    
    def save_to_csv(self, output_path: Union[str, Path]):
        """
        Save parsed data to CSV file.
        
        Args:
            output_path: Path for output CSV file
        """
        if self.data is None:
            raise ValueError("No data loaded. Run parse() first.")
        
        self.data.to_csv(output_path, index=False)
        print(f"Data saved to {output_path}")


def analyze_hidex_lsc_run(filepath: Union[str, Path], 
                         background_position: str = 'A01',
                         efficiency: float = 0.35,
                         sample_volume_ml: float = 1.0,
                         separator: str = ',') -> Dict:
    """
    Convenience function to analyze a complete Hidex LSC run.
    
    Args:
        filepath: Path to Hidex LSC CSV file
        background_position: Position containing background measurements
        efficiency: Counting efficiency (0-1)
        sample_volume_ml: Sample volume in mL
        separator: Field separator (',' or ';' or 'auto'). Default: ','
        
    Returns:
        Dictionary with complete analysis results
    """
    parser = HidexLSCParser(filepath, separator=separator)
    data = parser.parse()
    
    # Calculate statistics
    stats = parser.calculate_statistics()
    
    # Background correction
    bkg_corrected = parser.calculate_background_corrected_cpm(
        background_position=background_position
    )
    
    # Activity calculations
    activity = parser.calculate_dpm_and_activity(
        efficiency=efficiency,
        sample_volume_ml=sample_volume_ml
    )
    
    return {
        'parser': parser,
        'raw_data': data,
        'statistics': stats,
        'background_corrected': bkg_corrected,
        'activity': activity,
        'metadata': parser.metadata
    }


# Example usage
if __name__ == "__main__":
    print("Hidex LSC Tritium Parser loaded successfully!")
    print("\nUsage example:")
    print("=" * 70)
    print("""
from hidex_lsc_parser import HidexLSCParser, analyze_hidex_lsc_run

# Method 1: Simple parsing (comma-separated - default)
parser = HidexLSCParser('RUN11007_new.csv')
data = parser.parse()
print(data.head())

# Method 1b: Semicolon-separated file
parser = HidexLSCParser('RUN11007_new.csv', separator=';')
data = parser.parse()

# Method 1c: Auto-detect separator
parser = HidexLSCParser('RUN11007_new.csv', separator='auto')
data = parser.parse()
print(f"Detected separator: {parser.metadata['separator']}")

# Get statistics
stats = parser.calculate_statistics()
print(stats)

# Calculate activities
activity_data = parser.calculate_dpm_and_activity(
    efficiency=0.35,  # 35% efficiency
    sample_volume_ml=1.0
)
print(activity_data[['Pos', 'SampleType', 'CPMroi1', 'DPM', 'Bq_per_L']])

# Export to Excel
parser.export_summary('tritium_analysis.xlsx', efficiency=0.35)

# Method 2: Complete analysis
results = analyze_hidex_lsc_run(
    'RUN11007_new.csv',
    background_position='A01',
    efficiency=0.35,
    sample_volume_ml=1.0,
    separator='auto'  # Auto-detect separator
)

print("Background mean:", results['background_corrected']['Background_Mean'].iloc[0])
print("\\nActivity data:")
print(results['activity'][['Pos', 'SampleType', 'Bq_per_L']].head())
    """)
