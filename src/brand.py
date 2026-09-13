"""The product's name as users and third parties read it (issue #151).

One place, so a rename is one edit: email subjects and bodies, the GPX
``creator`` attribute, the API docs title and the User-Agent sent to
third-party services all read it from here.
"""

import os

APP_NAME = "TraxJourney"

REPO_URL = "https://github.com/rui-nar/TraxJourney"

# How the server identifies itself to third-party APIs (Overpass, Transitous,
# Nominatim). Their usage policies ask for a User-Agent naming the application
# and its version with a way to reach the operator; Overpass bans unverifiable
# ones by hand. The version is the build's real one (Dockerfile APP_VERSION,
# "dev" locally), not a frozen string, and the URL must be a repository that
# exists.
USER_AGENT = f"{APP_NAME}/{os.environ.get('APP_VERSION', 'dev')} (+{REPO_URL})"
