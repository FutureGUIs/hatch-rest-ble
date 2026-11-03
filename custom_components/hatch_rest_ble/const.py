"""Constants for the first-generation Hatch Rest."""

DOMAIN = "hatch_rest_ble"
MANUFACTURER_ID = 1076
MANUFACTURER_IDS = (MANUFACTURER_ID, 14099)
CHAR_TX = "02240002-5efd-47eb-9c1a-de53f7a2b232"
CHAR_FEEDBACK = "02260002-5efd-47eb-9c1a-de53f7a2b232"
CHAR_HARDWARE_REVISION = "00002a27-0000-1000-8000-00805f9b34fb"
COLOR_GRADIENT = (254, 254, 254)
CONF_POLL_INTERVAL = "poll_interval"
POLL_INTERVAL_SECONDS = 10
MIN_POLL_INTERVAL_SECONDS = 5
MAX_POLL_INTERVAL_SECONDS = 300
GATT_TIMEOUT = 10
DISCONNECT_TIMEOUT = 15
CACHE_CLEAR_TIMEOUT = 10
SESSION_TIMEOUT = 60
OPERATION_TIMEOUT = 165
RECOVERY_BACKOFF_SECONDS = 0.5
COMMAND_SETTLE_SECONDS = 0.5

SOUNDS: dict[int, str] = {
    0: "None",
    2: "Stream",
    3: "Pink noise",
    4: "Dryer",
    5: "Ocean",
    6: "Wind",
    7: "Rain",
    9: "Birds",
    10: "Crickets",
    11: "Brahms' lullaby",
    13: "Twinkle twinkle",
    14: "Rock-a-bye",
}
