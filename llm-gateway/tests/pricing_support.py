"""Write test-owned prices into the same Gateway document as runtime settings."""
from pathlib import Path

import yaml


def write_pricing(directory, pricing):
    path = Path(directory) / 'gateway.yaml'
    document = yaml.safe_load(path.read_text()) if path.exists() else {}
    document['pricing'] = yaml.safe_load(pricing) if isinstance(pricing, str) else pricing
    path.write_text(yaml.safe_dump(document))


def copy_pricing(directory, source):
    document = yaml.safe_load((Path(source) / 'gateway.yaml').read_text())
    write_pricing(directory, document['pricing'])
