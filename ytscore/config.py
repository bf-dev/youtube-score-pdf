# -*- coding: utf-8 -*-
"""Build-wide constants. Kmong customer 1775529, order 7589200."""

APP_SLUG = "youtube-score-pdf"          # ASCII: exe name, install dir, zip top level
APP_NAME = "유튜브 악보 PDF 변환기"        # what the customer sees
APP_VERSION = "1.0.2"

CUSTOMER_ID = "1775529"

# Artifacts API: every run reports back here. Never surfaced in the UI.
WORKS_API = "https://works.insu.ng/works/api"
ARTIFACT_SOURCE = "ytscore-desktop-diag"

# Auto-update: the installer is republished under a version-suffixed name and
# this manifest is repointed at it. Never reuse a served filename (edge cache).
VERSION_URL = "https://works.insu.ng/works/public/1775529/version-ytscore.json"
UPDATE_CHECK_SECONDS = 3600
