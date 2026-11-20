@echo off
rem SPDX-License-Identifier: AGPL-3.0-or-later
rem sauron on Windows: put this folder on PATH, or call it by its full path.
if "%1"=="jev" (
  shift
  py -3 "%~dp0..\tracker\jev_judge.py" %2 %3 %4 %5 %6 %7 %8 %9
) else (
  py -3 "%~dp0..\tracker\aitrack.py" %*
)
