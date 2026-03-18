from aiogram.fsm.state import State, StatesGroup


class SS(StatesGroup):
    """StickerStates — every step in the user flow."""
    MENU              = State()   # main menu visible
    WAIT_BG_COLOR     = State()   # awaiting background colour text
    WAIT_STICK_COLOR  = State()   # awaiting sticker colour text
    WAIT_WM_TEXT      = State()   # awaiting watermark text
    CHOOSE_FONT       = State()   # font picker (inline)
    CHOOSE_RESOLUTION = State()   # resolution picker (inline)
    CHOOSE_FPS        = State()   # fps picker (inline)
    CHOOSE_FORMAT     = State()   # format picker (inline)
    PROCESSING        = State()   # bot is rendering
