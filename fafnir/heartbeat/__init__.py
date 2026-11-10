"""Tier 5, the heartbeat: background checks that let Fafnir reach out first.

Checks write notices to data/notices.json; front ends (the desktop app) read that file,
show notices and speak the important ones. The heartbeat doesn't depend on any front end,
so it can later move to an always-on machine unchanged (`python main.py --heartbeat`).
"""
