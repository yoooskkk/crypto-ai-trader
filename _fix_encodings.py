import os
import re

# Files that need encoding="utf-8" fix
files_to_check = [
    "indicators/trend.py",
    "indicators/momentum.py",
    "indicators/volatility.py",
    "indicators/volume.py",
    "indicators/timeseries.py",
    "indicators/math_factors.py",
    "indicators/crypto_alpha.py",
    "data/rest_client.py",
    "ui/cli/timeframe_picker.py",
]

for filepath in files_to_check:
    # Read file
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Check if this file has the issue
    if "with open(cfg_path) as f:" in content:
        content = content.replace(
            "with open(cfg_path) as f:",
            'with open(cfg_path, encoding="utf-8") as f:'
        )
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Fixed: {filepath} (cfg_path)")
    elif "with open(CONFIG_PATH, \"r\") as f:" in content:
        content = content.replace(
            'with open(CONFIG_PATH, "r") as f:',
            'with open(CONFIG_PATH, "r", encoding="utf-8") as f:'
        )
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Fixed: {filepath} (CONFIG_PATH)")
    elif "with open(_CONFIG_PATH) as f:" in content:
        content = content.replace(
            "with open(_CONFIG_PATH) as f:",
            'with open(_CONFIG_PATH, encoding="utf-8") as f:'
        )
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Fixed: {filepath} (_CONFIG_PATH)")
    else:
        print(f"Skipped (already fixed): {filepath}")

print("\nDone!")
