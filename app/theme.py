"""Visual theme tokens for OCI Bot — Fluent light."""

from __future__ import annotations

# Brand
APP_TITLE = "OCI Bot"
APP_SUBTITLE = "Oracle Cloud 多租户实例管理面板"

# Layout — compact defaults so the instance table gets most of the space
WINDOW_MIN_SIZE = (960, 580)
WINDOW_DEFAULT_SIZE = (1120, 680)
SIDEBAR_WIDTH = 240          # default tenant list width (user can drag wider)
SIDEBAR_WIDTH_MIN = 160
SIDEBAR_WIDTH_MAX = 480
DETAIL_WIDTH = 252

# Fluent light palette (mirrors app.classic for any legacy reference)
COLORS = {
    "face": "#f3f3f3",
    "face_hover": "#ebebeb",
    "window": "#ffffff",
    "text": "#1a1a1a",
    "text_dim": "#424242",
    "text_mute": "#707070",
    "light": "#ffffff",
    "shadow": "#d6d6d6",
    "dark": "#4a4a4a",
    "select": "#cfe4fa",
    "select_fg": "#1a1a1a",
    "disabled": "#a0a0a0",
    "accent": "#0f6cbd",
}

# Instance lifecycle colors — Fluent-readable on white
STATE_COLORS = {
    "RUNNING": "#107c10",
    "STOPPED": "#707070",
    "STARTING": "#c43e1c",
    "STOPPING": "#c43e1c",
    "PROVISIONING": "#0f6cbd",
    "TERMINATING": "#c50f1f",
    "TERMINATED": "#a0a0a0",
    "MOVING": "#5c2d91",
    "CREATING_IMAGE": "#038387",
}

# Common regions for quick pick
COMMON_REGIONS = [
    "ap-tokyo-1",
    "ap-osaka-1",
    "ap-seoul-1",
    "ap-singapore-1",
    "ap-singapore-2",
    "ap-sydney-1",
    "ap-melbourne-1",
    "ap-mumbai-1",
    "ap-hyderabad-1",
    "ap-chuncheon-1",
    "us-ashburn-1",
    "us-phoenix-1",
    "us-sanjose-1",
    "us-chicago-1",
    "eu-frankfurt-1",
    "eu-amsterdam-1",
    "eu-london-1",
    "eu-paris-1",
    "eu-zurich-1",
    "uk-london-1",
    "ca-toronto-1",
    "ca-montreal-1",
    "sa-saopaulo-1",
    "me-jeddah-1",
    "me-dubai-1",
    "af-johannesburg-1",
    "il-jerusalem-1",
    "mx-queretaro-1",
]

# Per-tenant marker colors — Fluent-friendly distinct hues
TENANT_COLORS = [
    "#0f6cbd",
    "#107c10",
    "#c50f1f",
    "#5c2d91",
    "#038387",
    "#c43e1c",
    "#1a1a1a",
    "#004e8c",
    "#8a6116",
    "#9b2d6b",
]
