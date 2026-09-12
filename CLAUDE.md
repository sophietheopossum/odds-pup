# CLAUDE.md — `odds-pup` Development Guide

## Project Overview
`odds-pup` is a lightweight, high-performance desktop application designed for tracking, logging, and adjusting matched betting data (qualifying losses, free bet profits, and lay hedge adjustments).

---

## Core Architecture & Domain Logic

### 1. Data Model
* **Bet Entry Structure:**
  * `id`: Unique identifier (UUID).
  * `timestamp`: ISO-8601 date/time.
  * `event_name`: Description of match/event (e.g., "Arsenal vs Chelsea").
  * `bookmaker`: Bookmaker used for the back bet.
  * `exchange`: Exchange used for the lay bet.
  * `bet_type`: `QUALIFYING` | `FREE_BET_SNR` | `FREE_BET_SR` | `DUTCHING`.
  * `back_stake`: Numerical back stake value.
  * `back_odds`: Decimal odds on bookmaker.
  * `lay_stake`: Numerical lay stake value.
  * `lay_odds`: Decimal odds on exchange.
  * `lay_commission`: Percentage commission on exchange (e.g., `2.0` for 2%).
  * `expected_profit_loss`: Initial calculated expected profit (+) or qualifying loss (-).
  * `actual_profit_loss`: Realized profit/loss value after event settlement or lay adjustment.
  * `status`: `OPEN` | `SETTLED` | `ADJUSTED` | `CANCELLED`.
  * `notes`: Optional user notes or freeform comments.

### 2. Profit/Loss & Adjustment Logic
* **Qualifying Loss (QL) Calculation:**
  $$\text{QL} = (\text{Back Stake} \times (\text{Back Odds} - 1)) - (\text{Lay Stake} \times (\text{Lay Odds} - 1))$$
  *(Adjusted for exchange commission when back bet wins vs lay bet wins).*

* **Free Bet Profit (SNR) Calculation:**
  $$\text{Profit} = (\text{Back Stake} \times (\text{Back Odds} - 1)) - (\text{Lay Stake} \times (\text{Lay Odds} - 1))$$

* **Dynamic Adjustment Engine:**
  * When odds fluctuate or mid-match hedge adjustments occur, `odds-pup` recalculates the position.
  * If a qualifying loss is eliminated (e.g., via early payout, boosted odds cashout, or lay lock-in), update `actual_profit_loss` dynamically and set status to `ADJUSTED`.
  * Maintain audit trails for adjustments so original estimated QL vs actual final yield can be audited.

---

## Technology Stack Recommendations
* **GUI Framework:** Qt 6 (PyQt6 / PySide6) or Tauri / Web Technologies (for fast, lightweight native UI desktop experience).
* **Local Storage / Persistence:** SQLite or embedded JSON/SQLite database with atomic file writes to ensure data safety.
* **State Management:** Reactive store pattern for UI synchronization with local storage updates.

---

## Code Quality & Style Guidelines

### Formatting & Syntax
* Follow PEP 8 standards strictly if using Python; standard ESLint/Prettier configs if JS/TS runtime is present.
* Use explicit type annotations for all core domain functions, mathematical calculations, and storage interfaces.

### Error Handling & Data Integrity
* All calculation models must handle floating-point precision cleanly (prefer exact decimal types or round safely to 2 decimal places for visual output).
* Always validate inputs (`odds > 1.0`, `stake >= 0`, `commission >= 0%`).
* Ensure persistent storage operations are safe against sudden app shutdown or file locks.

---

## Primary Workflows

### Adding a New Bet Record
1. User inputs event details, back stake, odds, exchange commission, and bet type.
2. App computes expected QL or free bet profit in real-time before saving.
3. User confirms; entry appended to SQLite/storage with `OPEN` status.

### Adjusting an Existing Entry
1. Select an existing record from the ledger view.
2. Modify realized payout or overlay parameters (e.g., QL eliminated due to price shift or promo trigger).
3. The app updates `actual_profit_loss`, updates overall cumulative profit counters, and flags the entry.
