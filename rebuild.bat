@echo off
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\rebuild.ps1" %*
