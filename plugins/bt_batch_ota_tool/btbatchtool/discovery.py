"""Resolve names across short scans without reusing an old RSSI or presence."""
import re

from .settings import name_rule


class ScanNames:
    def __init__(self, rule, clock, max_age=120, limit=256):
        self.clock, self.max_age, self.limit = clock, max_age, limit
        self.recent = {}
        self.expected = {}
        prefix, names = name_rule(rule)
        for name in names or (prefix,):
            match = re.fullmatch(r'XFN_([0-9a-fA-F]{12})', name)
            if match:
                mac = match.group(1).upper()
                self.expected[':'.join(mac[i:i + 2] for i in range(0, 12, 2))] = name

    def resolve(self, row):
        address, name, rssi = row
        key, now = address.upper(), self.clock()
        if name:
            self.recent[key] = (now, name)
            while len(self.recent) > self.limit:
                del self.recent[min(self.recent, key=lambda k: self.recent[k][0])]
        else:
            known = self.recent.get(key)
            if known and now - known[0] <= self.max_age:
                name = known[1]
            elif known:
                del self.recent[key]
            # Only a full name explicitly supplied by the user can authorize
            # this address fallback. A generic XFN_ prefix never guesses names.
            if not name:
                name = self.expected.get(key, '')
        return address, name, rssi
