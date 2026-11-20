"""Conservative labelled-field extraction, never infer identity from an email address."""
import re


def single_label(body, patterns, validator):
    values = []
    for pattern in patterns:
        for value in re.findall(pattern, body, re.I | re.M):
            value = value.strip()
            if value.casefold() not in ('n/a','na','none','unknown','not provided','not available','-') and validator(value):
                values.append(value)
    unique = {v.casefold(): v for v in values}
    return next(iter(unique.values())) if len(unique) == 1 else None


def extract(body):
    return {
        'alumnus_name': single_label(body, [r'^\s*(?:full name|alumnus name|name)\s*:\s*([^\r\n]+)$'],
                                    lambda v: 1 < len(v) <= 120 and all(c.isalpha() or c in " .'-" for c in v)),
        'enrolment_number': single_label(body, [r'^\s*(?:enrol(?:l)?ment\s*(?:number|no\.?|id)|student\s*(?:number|id))\s*:\s*([^\r\n]+)$'],
                                        lambda v: bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/-]{0,49}', v))),
        'alumni_number': single_label(body, [r'^\s*alumni\s*(?:number|no\.?|id)\s*:\s*([^\r\n]+)$'],
                                     lambda v: bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/-]{0,49}', v))),
    }
