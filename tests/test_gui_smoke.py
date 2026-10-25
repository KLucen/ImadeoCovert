"""Construct the original image GUI without touching the user's settings."""

def test_image_app_starts_with_isolated_settings(tmp_path, monkeypatch):
    import formatix

    monkeypatch.setattr(formatix, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(formatix, "load_settings", lambda: {"check_updates": False, "lang": "en"})
    app = formatix.App()
    try:
        app.withdraw()
        app.update_idletasks()
        app.update()
        assert app.title() == formatix.APP_NAME
        assert app._fmt.get() in formatix.FORMATS
        assert not app._running
        assert not app._check_updates.get()
        assert all(formatix.STRINGS[lang].get("convert_btn") for lang in formatix.LANGUAGES)
    finally:
        for callback in app.tk.call("after", "info"):
            app.after_cancel(callback)
        app.destroy()
