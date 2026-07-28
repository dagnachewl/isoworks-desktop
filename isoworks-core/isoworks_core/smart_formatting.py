"""
Smart Formatting for Scientific Data
=====================================

Provides intelligent formatting functions for numerical values following
scientific conventions for significant figures and uncertainty representation.

Principles:
-----------
1. Uncertainty determines precision (Particle Data Group conventions)
2. Uncertainty rounded to 1-2 significant figures
3. Value rounded to same decimal place as uncertainty
4. Magnitude-aware formatting for standalone values
5. Avoids false precision (e.g., 12.500000 when uncertainty is ±2)

Author: TRIMS Development Team
Date: 2025-02-25
"""
from __future__ import annotations

import math
from typing import Optional, Tuple, Union
import numpy as np


def smart_round_value(
    value: Union[float, int],
    min_decimals: int = 0,
    max_decimals: int = 6
) -> float:
    """
    Intelligently round a value based on its magnitude.
    
    Uses adaptive decimal places based on the value's size:
    - Very small values (< 0.01): More decimal places
    - Small values (< 1): Moderate decimal places  
    - Medium values (1-100): Fewer decimal places
    - Large values (> 100): Minimal decimal places
    
    Args:
        value: Number to round
        min_decimals: Minimum decimal places (default: 0)
        max_decimals: Maximum decimal places (default: 6)
        
    Returns:
        Rounded value
        
    Examples:
        >>> smart_round_value(0.002554)
        0.0026
        >>> smart_round_value(25.3424)
        25.34
        >>> smart_round_value(1234.567)
        1235.0
        >>> smart_round_value(0.000123456)
        0.00012
    """
    if value == 0:
        return 0.0
    
    abs_val = abs(value)
    
    # Determine appropriate decimal places based on magnitude
    if abs_val < 0.001:
        # Very small: keep 5-6 significant figures
        decimals = -int(math.floor(math.log10(abs_val))) + 4
    elif abs_val < 0.01:
        # Small: 4 decimal places
        decimals = 4
    elif abs_val < 0.1:
        # Medium-small: 3 decimal places
        decimals = 3
    elif abs_val < 1:
        # Less than 1: 2 decimal places
        decimals = 2
    elif abs_val < 10:
        # 1-10: 2 decimal places
        decimals = 2
    elif abs_val < 100:
        # 10-100: 2 decimal places
        decimals = 2
    elif abs_val < 1000:
        # 100-1000: 1 decimal place
        decimals = 1
    else:
        # Large numbers: 0-1 decimal places
        decimals = 0
    
    # Apply min/max constraints
    decimals = max(min_decimals, min(decimals, max_decimals))
    
    return round(value, decimals)


def format_value_uncertainty(
    value: float,
    uncertainty: Optional[float] = None,
    force_decimals: Optional[int] = None
) -> Tuple[float, Optional[float]]:
    """
    Format value-uncertainty pair following scientific conventions.
    
    Implements Particle Data Group (PDG) guidelines:
    - Uncertainty rounded to 1-2 significant figures
    - Value rounded to same decimal place as uncertainty
    - Avoids false precision
    
    Rules:
    ------
    1. If uncertainty has leading digit 1 or 2: keep 2 significant figures
       Example: 0.186 → 0.19, not 0.2
    2. Otherwise: keep 1 significant figure
       Example: 0.354 → 0.4
    3. Round value to same decimal place as uncertainty
    
    Args:
        value: The measured value
        uncertainty: The uncertainty (standard deviation, etc.)
        force_decimals: Override automatic decimal determination
        
    Returns:
        Tuple of (rounded_value, rounded_uncertainty)
        
    Examples:
        >>> format_value_uncertainty(12.5432, 0.186)
        (12.54, 0.19)
        >>> format_value_uncertainty(0.002554, 0.000123)
        (0.00255, 0.00012)
        >>> format_value_uncertainty(1234.567, 45.3)
        (1235.0, 45.0)
        >>> format_value_uncertainty(0.4532, 0.0089)
        (0.453, 0.009)
        >>> format_value_uncertainty(25.3424, None)  # No uncertainty
        (25.34, None)
    """
    # Handle no uncertainty case
    if uncertainty is None or uncertainty == 0:
        if force_decimals is not None:
            return (round(value, force_decimals), None)
        else:
            return (smart_round_value(value), None)
    
    # Handle force_decimals
    if force_decimals is not None:
        return (
            round(value, force_decimals),
            round(uncertainty, force_decimals)
        )
    
    # Get order of magnitude of uncertainty
    if uncertainty == 0:
        return (smart_round_value(value), 0.0)
    
    abs_unc = abs(uncertainty)
    
    # Find the most significant digit of uncertainty
    if abs_unc >= 1:
        # For uncertainty >= 1
        log_unc = math.log10(abs_unc)
        exponent = int(math.floor(log_unc))
        mantissa = abs_unc / (10 ** exponent)
    else:
        # For uncertainty < 1
        log_unc = math.log10(abs_unc)
        exponent = int(math.floor(log_unc))
        mantissa = abs_unc / (10 ** exponent)
    
    # Determine significant figures for uncertainty
    # PDG rule: If leading digit is 1 or 2, keep 2 sig figs; otherwise 1
    if mantissa < 3:
        # Leading digit is 1 or 2: keep 2 significant figures
        sig_figs = 2
    else:
        # Leading digit is 3-9: keep 1 significant figure
        sig_figs = 1
    
    # Round uncertainty to appropriate sig figs
    if abs_unc >= 1:
        # For values >= 1, round to appropriate power of 10
        round_to = 10 ** (exponent - sig_figs + 1)
        rounded_unc = round(abs_unc / round_to) * round_to
        # Determine decimal places
        decimals = max(0, sig_figs - exponent - 1)
    else:
        # For values < 1, work with decimal places
        decimals = -exponent + sig_figs - 1
        rounded_unc = round(abs_unc, decimals)
    
    # Round value to same decimal places
    rounded_val = round(value, decimals)
    
    return (rounded_val, rounded_unc)


def format_for_database(
    value: float,
    uncertainty: Optional[float] = None,
    force_decimals: Optional[int] = None
) -> Union[float, Tuple[float, float]]:
    """
    Format numerical value(s) for database storage.
    
    This is the main function to use when saving data to database.
    Ensures consistent, scientifically appropriate precision.
    
    Args:
        value: The value to format
        uncertainty: Optional uncertainty value
        force_decimals: Optional override for decimal places
        
    Returns:
        Formatted value (or tuple if uncertainty provided)
        
    Examples:
        >>> format_for_database(12.5678, 0.23)
        (12.57, 0.23)
        >>> format_for_database(0.002554)
        0.0026
        >>> format_for_database(1234.567, 45.3)
        (1235.0, 45.0)
    """
    if uncertainty is not None:
        return format_value_uncertainty(value, uncertainty, force_decimals)
    else:
        if force_decimals is not None:
            return round(value, force_decimals)
        else:
            return smart_round_value(value)


def format_snapshot_parameters(params: dict) -> dict:
    """
    Format all numerical parameters in a snapshot dictionary.
    
    Applies smart formatting to common parameter names while preserving
    value-uncertainty pairs.
    
    Common patterns detected:
    - *_unc, *_uncertainty, *_error: Paired with base value
    - background_cpm, efficiency, chi_squared: Standalone values
    
    Args:
        params: Dictionary of parameters (e.g., fit_parameters)
        
    Returns:
        Dictionary with formatted values
        
    Example:
        >>> params = {
        ...     'background_cpm': 12.5432,
        ...     'background_cpm_uncertainty': 0.186,
        ...     'efficiency': 0.4532,
        ...     'efficiency_uncertainty': 0.0089,
        ...     'chi_squared': 1.2345,
        ...     'outliers_removed': 2,
        ...     'samples_processed': 24
        ... }
        >>> formatted = format_snapshot_parameters(params)
        >>> formatted['background_cpm']
        12.54
        >>> formatted['background_cpm_uncertainty']
        0.19
    """
    if not params:
        return params
    
    formatted = {}
    processed_keys = set()
    
    # First pass: Handle value-uncertainty pairs
    for key, value in params.items():
        if key in processed_keys:
            continue
        
        # Check if this is an uncertainty field
        if any(suffix in key for suffix in ['_unc', '_uncertainty', '_error']):
            # Find the base value key
            base_key = key.replace('_uncertainty', '').replace('_unc', '').replace('_error', '')
            
            if base_key in params and isinstance(params[base_key], (int, float, np.number)):
                # Format the pair
                base_val = float(params[base_key])
                unc_val = float(value)
                
                formatted_val, formatted_unc = format_value_uncertainty(base_val, unc_val)
                
                formattedbase_key = formatted_val
                formattedkey = formatted_unc
                processed_keys.add(base_key)
                processed_keys.add(key)
            else:
                # No base value found, format standalone
                if isinstance(value, (int, float, np.number)):
                    formattedkey = smart_round_value(float(value))
                else:
                    formattedkey = value
                processed_keys.add(key)
        
        elif isinstance(value, (int, float, np.number)) and key not in processed_keys:
            # Check if there's a matching uncertainty
            unc_keys = [
                f"{key}_uncertainty",
                f"{key}_unc",
                f"{key}_error"
            ]
            
            unc_key = None
            for uk in unc_keys:
                if uk in params:
                    unc_key = uk
                    break
            
            if unc_key:
                # Will be handled in first pass when we hit the uncertainty key
                continue
            else:
                # Standalone value
                formattedkey = smart_round_value(float(value))
                processed_keys.add(key)
        
        elif not isinstance(value, (int, float, np.number)):
            # Non-numeric value (string, None, etc.)
            formattedkey = value
            processed_keys.add(key)
    
    return formatted


# Convenience functions for common use cases

def format_background(cpm: float, uncertainty: float) -> Tuple[float, float]:
    """Format background CPM and uncertainty."""
    return format_value_uncertainty(cpm, uncertainty)


def format_efficiency(eff: float, uncertainty: float) -> Tuple[float, float]:
    """Format efficiency and uncertainty."""
    return format_value_uncertainty(eff, uncertainty)


def format_activity(activity: float, uncertainty: float) -> Tuple[float, float]:
    """Format activity and uncertainty."""
    return format_value_uncertainty(activity, uncertainty)


def format_chi_squared(chi_sq: float) -> float:
    """Format chi-squared value (typically 2-3 decimal places)."""
    return round(chi_sq, 3)


# Export main functions
__all__ = [
    'smart_round_value',
    'format_value_uncertainty',
    'format_for_database',
    'format_snapshot_parameters',
    'format_background',
    'format_efficiency',
    'format_activity',
    'format_chi_squared',
]
