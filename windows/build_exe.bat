@echo off
py -m pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name TenzXcraft tenzxcraft.py
