"""Render a deployment-specific Outlook manifest; never invent an origin or app ID."""
import argparse
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET


def render(origin, addin_id):
    parsed = urlsplit(origin)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.path not in ('', '/') or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('An HTTPS origin without credentials, path or query is required')
    app_id = str(UUID(addin_id))
    template = Path(__file__).resolve().parent.parent / 'addin/manifest.template.xml'
    result = template.read_text().replace('{{ADDIN_ID}}', app_id).replace('{{BASE_URL}}', escape(origin.rstrip('/'), {'"':'&quot;'}))
    ET.fromstring(result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--origin', required=True)
    parser.add_argument('--id', required=True, help='Stable add-in manifest UUID; distinct from Entra client IDs')
    parser.add_argument('--output', default='build/manifest.xml')
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(args.origin, args.id))
    print('Manifest written to ' + str(output))
