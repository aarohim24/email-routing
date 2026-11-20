"""Conservative deterministic classifier. Replace classify via the same result contract."""
import re
from dataclasses import dataclass
from html.parser import HTMLParser


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('br', 'p', 'div', 'li'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(content, content_type='text'):
    if content_type.lower() != 'html':
        return content
    parser = PlainText()
    parser.feed(content)
    return ''.join(parser.parts)


PATTERNS = {
    'TRANSCRIPT': r'\btranscripts?\b',
    'PROVISIONAL_DEGREE': r'\bprovisional\s+(?:degree|certificate)\b',
    'DEGREE_VERIFICATION': r'\b(?:degree\s+verification|verify\s+(?:my\s+|the\s+)?degree|verification\s+of\s+(?:my\s+|the\s+)?degree)\b',
    'MEDIUM_OF_INSTRUCTION': r'\bmedium\s+of\s+instruction\b|\bMOI\b',
    'DEGREE_COLLECTION': r'\b(?:degree\s+collection|collect\s+(?:my\s+|the\s+)?degree|collection\s+of\s+(?:my\s+|the\s+)?degree)\b',
    'MIGRATION': r'\bmigration\b',
}


@dataclass(frozen=True)
class Classification:
    category: str
    matches: tuple
    reason: str


def classify(subject, body):
    text = subject + '\n' + body
    matches = tuple(k for k, pattern in PATTERNS.items() if re.search(pattern, text, re.I))
    outside_scope = re.search(r'\b(?:grade\s+cards?|letters?\s+of\s+recommendation|recommendation\s+letters?)\b',text,re.I)
    negated = re.search(r"\b(?:not|don't|do not|no longer)\b.{0,40}(?:degree|transcript|migration|instruction)", text, re.I)
    if len(matches) != 1 or negated or outside_scope:
        return Classification('UNKNOWN', matches, 'ambiguous, negated or unmatched concern')
    return Classification(matches[0], matches, 'single explicit concern match')


def resolution_reply(body):
    # Only an exact, unquoted answer is automatic. Natural language stays with admin.
    answer = body.strip().upper()
    if answer == 'RESOLVED':
        return True
    if answer == 'NOT RESOLVED':
        return False
    return None

# Initial pilot scope. Other documentation types are intentionally manual review.
PILOT_SCOPE = tuple(PATTERNS)
BASELINE_VERSION = 'rules-v2'


class RuleBasedClassifier:
    version = BASELINE_VERSION

    def classify(self, subject, body):
        return classify(subject, body)
