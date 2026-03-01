import os
import json
import pytest
from TerraLab.common.utils import get_config_value, set_config_value, _clear_cache_config, getTraduction

def test_set_and_get_config():
    """Test that a value set in config can be retrieved."""
    # Ensure fresh state
    _clear_cache_config()
    test_key = "test_dummy_key"
    test_val = "hello_world"
    
    set_config_value(test_key, test_val)
    retrieved = get_config_value(test_key, "default_fallback")
    
    assert retrieved == test_val

def test_config_persistence():
    """Test that values are actually saved to disk."""
    _clear_cache_config()
    test_key = "test_persistence_key"
    test_val = 12345
    
    set_config_value(test_key, test_val)
    
    # Read the file directly to verify
    from TerraLab.common.utils import resource_path
    path = resource_path("data/config.json")
    
    assert os.path.exists(path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    assert data.get(test_key) == test_val

def test_default_language():
    """Test translation fallback mechanism."""
    _clear_cache_config()
    # Force language to something without translations
    set_config_value("idioma", "unknown_lang")
    
    # "Astro.Stars" has translations in ca, es, en, fr, it. But we ask for 'unknown_lang'.
    # getTraduction uses the default string if strict key/lang doesn't exist
    result = getTraduction("Astro.Stars", "Fallback Default")
    
    assert result == "Fallback Default"
