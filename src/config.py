"""
Configuration loader for sndbx
Loads and parses .env and config.json5 files
"""

import os
import json
import json5 as json5_lib
from pathlib import Path
from typing import Dict, Any, Optional
import re


class ConfigError(Exception):
    """Configuration loading error"""
    pass


def load_env_file(path: str) -> Dict[str, str]:
    """Load .env file into dictionary"""
    env_vars = {}
    if not os.path.exists(path):
        return env_vars
    
    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, value = line.split('=', 1)
                env_vars[key.strip()] = value.strip()
    
    return env_vars


def expand_placeholders(value: Any, env_vars: Dict[str, str]) -> Any:
    """Expand ${VAR:-default} and ${VAR} placeholders in config values"""
    if not isinstance(value, str):
        return value
    
    # Replace ${VAR:-default} and ${VAR}
    def replace_var(match):
        var_name = match.group(1)
        default_val = match.group(2) if match.group(2) else None
        
        # First check environment
        if var_name in os.environ:
            return os.environ[var_name]
        # Then check loaded .env vars
        if var_name in env_vars:
            return env_vars[var_name]
        # Use default if provided
        if default_val is not None:
            return default_val
        # Raise error if no default and not found
        raise ConfigError(f"Placeholder ${{{var_name}}} not found in environment or .env")
    
    # Pattern: ${VAR} or ${VAR:-default}
    pattern = r'\$\{([A-Z_][A-Z0-9_]*?)(?::-(.*?))?\}'
    result = re.sub(pattern, replace_var, value)
    return result


def load_config_json5(path: str, env_vars: Dict[str, str]) -> Dict[str, Any]:
    """Load config.json5 file with placeholder expansion"""
    if not os.path.exists(path):
        raise ConfigError(f"Config file not found: {path}")
    
    with open(path, 'r') as f:
        content = f.read()
    
    # Expand placeholders in the entire file before parsing
    def replace_var(match):
        var_name = match.group(1)
        default_val = match.group(2) if match.group(2) else None
        
        # First check environment
        if var_name in os.environ:
            return os.environ[var_name]
        # Then check loaded .env vars
        if var_name in env_vars:
            return env_vars[var_name]
        # Use default if provided
        if default_val is not None:
            return default_val
        # Raise error if no default and not found
        raise ConfigError(f"Placeholder ${{{var_name}}} not found in environment or .env")
    
    # Pattern: ${VAR} or ${VAR:-default}
    pattern = r'\$\{([A-Z_][A-Z0-9_]*?)(?::-(.*?))?\}'
    content = re.sub(pattern, replace_var, content)
    
    # Parse JSON5
    try:
        config = json5_lib.loads(content)
    except ValueError as e:
        raise ConfigError(f"Invalid JSON5 in config: {e}")
    except Exception as e:
        raise ConfigError(f"Error parsing JSON5: {e}")
    
    return config


def load_config(root_dir: str = '.') -> Dict[str, Any]:
    """Load complete sndbx configuration"""
    env_path = os.path.join(root_dir, '.env')
    config_path = os.path.join(root_dir, 'config.json5')
    
    # Load .env first
    env_vars = load_env_file(env_path)
    
    # Set SNDBX_ROOT if not present
    if 'SNDBX_ROOT' not in env_vars:
        env_vars['SNDBX_ROOT'] = os.path.abspath(root_dir)
    
    # Load config
    config = load_config_json5(config_path, env_vars)
    
    return {
        'env': env_vars,
        'config': config
    }
