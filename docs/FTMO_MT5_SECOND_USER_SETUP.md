# FTMO MT5 Second-User Setup

Use a second macOS user for the FTMO terminal. Do not try to install a second separate `MetaTrader 5.app`.

## Why

The app bundle can stay shared. The isolation comes from the user home:

- current user -> GFT MT5 files
- second user -> FTMO MT5 files

That gives separate:

- `~/Library/Application Support/...`
- Wine/MetaQuotes data
- `MQL5/Files`
- DWX bridge files

## Steps

1. Open `System Settings -> Users & Groups`.
2. Add a new standard user for FTMO.
3. Enable fast user switching.
4. Log into the FTMO user once.
5. Launch the existing `MetaTrader 5.app` inside that FTMO user session.
6. Log into the FTMO demo account in that terminal.
7. Confirm the DWX EA is writing files under that user’s MT5 `MQL5/Files` folder.

## Path To Capture

Expected pattern:

`/Users/<ftmo_user>/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/MQL5/Files`

That value should be exported as:

`FTMO_DWX_FILES_PATH`

## Current QA Runtime Expectation

- profile id: `ftmo_demo_100k`
- account number: `1513073754`
- context source: `ftmo_mt5_local`
- execution bridge: `mt5_local_ftmo_demo_100k`
- symbol suffix for forex/crypto: blank

## What To Do After You Have The Path

1. Set `FTMO_DWX_FILES_PATH`.
2. Flip `context_sources.ftmo_mt5_local.enabled = true`.
3. Flip `context_sources.ftmo_mt5_local.profiles.ftmo_demo_100k.enabled = true`.
4. Flip `profiles.ftmo_demo_100k.is_active = true`.
5. For tomorrow’s automated experiment, disable GFT from the automated run if FTMO is the only live broker truth we want in the loop.
