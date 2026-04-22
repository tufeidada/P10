# P6/P4 DuckDB Schema (agu.duckdb)

Total tables: 29

## backtest_results (5 rows)

| Column | Type | Nullable |
|--------|------|----------|
| id | BIGINT | NO |
| strategy | VARCHAR | YES |
| start_date | DATE | YES |
| end_date | DATE | YES |
| hold_days | INTEGER | YES |
| annual_return_pct | FLOAT | YES |
| sharpe | FLOAT | YES |
| win_rate_pct | FLOAT | YES |
| p_value | FLOAT | YES |
| run_time | TIMESTAMP | YES |
| report_json | VARCHAR | YES |
| curve_json | VARCHAR | YES |

## chanlun_signals (1,697 rows)

| Column | Type | Nullable |
|--------|------|----------|
| signal_id | VARCHAR | NO |
| symbol | VARCHAR | NO |
| trade_date | DATE | NO |
| check_time | TIMESTAMP | NO |
| timeframe | VARCHAR | NO |
| has_signal | BOOLEAN | NO |
| signal_type | VARCHAR | NO |
| signal_direction | VARCHAR | YES |
| signal_strength | INTEGER | YES |
| bi_count | INTEGER | YES |
| xd_direction | VARCHAR | YES |
| zs_exists | BOOLEAN | YES |
| zs_zg | DOUBLE | YES |
| zs_zd | DOUBLE | YES |
| current_price | DOUBLE | YES |
| last_bi_high | DOUBLE | YES |
| last_bi_low | DOUBLE | YES |
| is_divergence | BOOLEAN | YES |
| macd_dif | DOUBLE | YES |
| macd_dea | DOUBLE | YES |
| fx_type | VARCHAR | YES |
| reason | VARCHAR | YES |
| extra_json | VARCHAR | YES |
| created_at | TIMESTAMP | YES |

## chanlun_stats_daily (6 rows)

| Column | Type | Nullable |
|--------|------|----------|
| trade_date | DATE | NO |
| total_checks | INTEGER | NO |
| s1_count | INTEGER | NO |
| s2_count | INTEGER | NO |
| s3_count | INTEGER | NO |
| b1_count | INTEGER | NO |
| b2_count | INTEGER | NO |
| b3_count | INTEGER | NO |
| no_signal_count | INTEGER | NO |
| symbols_checked | INTEGER | NO |
| created_at | TIMESTAMP | YES |

## daily_plan (288 rows)

| Column | Type | Nullable |
|--------|------|----------|
| trade_date | DATE | NO |
| symbol | VARCHAR | NO |
| plan_type | VARCHAR | NO |
| watch_priority | VARCHAR | YES |
| trend_state | VARCHAR | YES |
| setup_type | VARCHAR | YES |
| trigger_price | DECIMAL(12,4) | YES |
| trigger_zone_low | DECIMAL(12,4) | YES |
| trigger_zone_high | DECIMAL(12,4) | YES |
| invalidation_price | DECIMAL(12,4) | YES |
| hold_score | INTEGER | YES |
| exit_risk_score | DOUBLE | YES |
| action_hint | VARCHAR | YES |
| notes | VARCHAR | YES |
| created_at | TIMESTAMP | YES |
| rank_score | DOUBLE | YES |
| rank_pct | DOUBLE | YES |
| risk_prob | DOUBLE | YES |
| risk_tier | VARCHAR | YES |
| down_prob | DOUBLE | YES |
| down_tier | VARCHAR | YES |
| market_regime | VARCHAR | YES |
| bear_gated | BOOLEAN | YES |
| is_top_pick | BOOLEAN | YES |
| cl_daily_phase | VARCHAR | YES |
| cl_hourly_phase | VARCHAR | YES |
| cl_combined | VARCHAR | YES |
| cl_action_hint | VARCHAR | YES |
| cl_has_buy | BOOLEAN | YES |
| cl_has_sell | BOOLEAN | YES |
| suggested_position_pct | DOUBLE | YES |
| risk_total | DOUBLE | YES |
| risk_bucket | VARCHAR | YES |
| rank_pct_v2 | DOUBLE | YES |
| candidate_tier | VARCHAR | YES |
| is_risk_filtered | BOOLEAN | YES |
| position_cap | DOUBLE | YES |

## dq_report (0 rows)

| Column | Type | Nullable |
|--------|------|----------|
| trade_date | DATE | NO |
| check_time | TIMESTAMP | NO |
| results_json | VARCHAR | NO |

## experiment_log (14 rows)

| Column | Type | Nullable |
|--------|------|----------|
| id | INTEGER | NO |
| experiment_id | VARCHAR | NO |
| model_type | VARCHAR | NO |
| feature_version | VARCHAR | NO |
| train_start | VARCHAR | YES |
| train_end | VARCHAR | YES |
| val_start | VARCHAR | YES |
| val_end | VARCHAR | YES |
| hyperparams_json | VARCHAR | YES |
| metrics_json | VARCHAR | YES |
| data_hash | VARCHAR | YES |
| notes | VARCHAR | YES |
| created_at | TIMESTAMP | NO |

## features_daily (5,440,027 rows)

| Column | Type | Nullable |
|--------|------|----------|
| symbol | VARCHAR | NO |
| trade_date | DATE | NO |
| feature_version | VARCHAR | YES |
| f_ma5_dev | DOUBLE | YES |
| f_ma10_dev | DOUBLE | YES |
| f_ma20_dev | DOUBLE | YES |
| f_ma60_dev | DOUBLE | YES |
| f_ma10_slope | DOUBLE | YES |
| f_ma5_above_ma20 | BOOLEAN | YES |
| f_ma20_above_ma60 | BOOLEAN | YES |
| f_ret_1d | DOUBLE | YES |
| f_ret_3d | DOUBLE | YES |
| f_ret_5d | DOUBLE | YES |
| f_ret_10d | DOUBLE | YES |
| f_ret_20d | DOUBLE | YES |
| f_momentum_accel5 | DOUBLE | YES |
| f_atr_14 | DOUBLE | YES |
| f_atr_ratio | DOUBLE | YES |
| f_hv_10 | DOUBLE | YES |
| f_hv_20 | DOUBLE | YES |
| f_rsi_6 | DOUBLE | YES |
| f_rsi_14 | DOUBLE | YES |
| f_rsi_overbought | BOOLEAN | YES |
| f_rsi_oversold | BOOLEAN | YES |
| f_macd_dif | DOUBLE | YES |
| f_macd_dea | DOUBLE | YES |
| f_macd_hist | DOUBLE | YES |
| f_macd_hist_slope | DOUBLE | YES |
| f_macd_golden_cross | BOOLEAN | YES |
| f_macd_dead_cross | BOOLEAN | YES |
| f_kdj_k | DOUBLE | YES |
| f_kdj_d | DOUBLE | YES |
| f_kdj_j | DOUBLE | YES |
| f_kdj_overbought | BOOLEAN | YES |
| f_kdj_oversold | BOOLEAN | YES |
| f_boll_position | DOUBLE | YES |
| f_boll_width | DOUBLE | YES |
| f_vol_ratio_5d | DOUBLE | YES |
| f_vol_up_flag | BOOLEAN | YES |
| f_dist_20d_high | DOUBLE | YES |
| f_dist_20d_low | DOUBLE | YES |
| f_alpha_hs300_5d | DOUBLE | YES |
| f_alpha_zz1000_5d | DOUBLE | YES |
| f_close | DOUBLE | YES |
| f_atr_14_raw | DOUBLE | YES |
| f_max_high_20d | DOUBLE | YES |
| f_min_low_20d | DOUBLE | YES |
| created_at | TIMESTAMP | YES |
| f_ret_60d | DOUBLE | YES |
| f_close_pct_20d_range | DOUBLE | YES |
| f_close_pct_60d_range | DOUBLE | YES |
| f_open_gap | DOUBLE | YES |
| f_close_pos_intraday | DOUBLE | YES |
| f_amount_ratio_5d | DOUBLE | YES |
| f_up_days_5d | DOUBLE | YES |
| f_pe_ttm | DOUBLE | YES |
| f_pb | DOUBLE | YES |
| f_ps_ttm | DOUBLE | YES |
| f_max_high_60d | DOUBLE | YES |
| f_min_low_60d | DOUBLE | YES |
| f_ret_lag1_1d | DOUBLE | YES |
| f_vol_rank_60d | DOUBLE | YES |
| f_roe_ttm | DOUBLE | YES |
| f_or_yoy | DOUBLE | YES |
| f_turnover_rate_f | DOUBLE | YES |
| f_atr_ratio_rank_60d | DOUBLE | YES |
| f_amount_rank_60d | DOUBLE | YES |
| f_turnover_rank_20d | DOUBLE | YES |
| f_days_since_report | DOUBLE | YES |
| f_beta_60d | DOUBLE | YES |
| f_idio_vol_20d | DOUBLE | YES |
| f_amihud_20d | DOUBLE | YES |

## financials_quarterly (136,836 rows)

| Column | Type | Nullable |
|--------|------|----------|
| symbol | VARCHAR | NO |
| period | VARCHAR | NO |
| ann_date | VARCHAR | YES |
| roe_ttm | DOUBLE | YES |
| or_yoy | DOUBLE | YES |
| data_source | VARCHAR | NO |

## fundamentals_daily (5,451,553 rows)

| Column | Type | Nullable |
|--------|------|----------|
| symbol | VARCHAR | NO |
| trade_date | DATE | NO |
| pe_ttm | DOUBLE | YES |
| pb | DOUBLE | YES |
| ps_ttm | DOUBLE | YES |
| total_mv_yi | DOUBLE | YES |
| circ_mv_yi | DOUBLE | YES |
| dv_ratio | DOUBLE | YES |
| data_source | VARCHAR | NO |
| turnover_rate_f | DOUBLE | YES |
| moneyflow_net_pct | DOUBLE | YES |
| moneyflow_large_pct | DOUBLE | YES |
| margin_balance_chg | DOUBLE | YES |
| limit_up_count | INTEGER | YES |
| limit_down_count | INTEGER | YES |
| limit_up_down_ratio | DOUBLE | YES |
| margin_balance_rank | DOUBLE | YES |
| block_trade_premium | DOUBLE | YES |

## hint_performance (653 rows)

| Column | Type | Nullable |
|--------|------|----------|
| score_id | INTEGER | NO |
| symbol | VARCHAR | NO |
| eval_time | TIMESTAMP | NO |
| score_at_eval | INTEGER | YES |
| actual_ret_1h | DOUBLE | YES |
| actual_ret_4h | DOUBLE | YES |
| actual_ret_1d | DOUBLE | YES |
| actual_ret_3d | DOUBLE | YES |
| filled_at | TIMESTAMP | YES |

## industry_classify (5,190 rows)

| Column | Type | Nullable |
|--------|------|----------|
| symbol | VARCHAR | NO |
| sw1_code | VARCHAR | YES |
| sw1_name | VARCHAR | YES |
| sw2_code | VARCHAR | YES |
| sw2_name | VARCHAR | YES |
| updated_at | TIMESTAMP | YES |

## intraday_features (44 rows)

| Column | Type | Nullable |
|--------|------|----------|
| symbol | VARCHAR | NO |
| bar_time | TIMESTAMP | NO |
| timeframe | VARCHAR | NO |
| hourly_ret_1h | DOUBLE | YES |
| hourly_vol_ratio | DOUBLE | YES |
| vwap_deviation | DOUBLE | YES |
| upper_shadow_ratio | DOUBLE | YES |
| intraday_alpha_vs_hs300 | DOUBLE | YES |
| sector_momentum_1h | DOUBLE | YES |
| atr_expanding | BOOLEAN | YES |
| distance_to_stop_pct | DOUBLE | YES |
| trailing_activated | BOOLEAN | YES |
| limit_up_count | INTEGER | YES |
| limit_down_count | INTEGER | YES |
| north_net_flow_100m | DOUBLE | YES |
| open | DOUBLE | YES |
| high | DOUBLE | YES |
| low | DOUBLE | YES |
| close | DOUBLE | YES |
| volume | BIGINT | YES |

## intraday_scores (772 rows)

| Column | Type | Nullable |
|--------|------|----------|
| score_id | INTEGER | NO |
| symbol | VARCHAR | NO |
| eval_time | TIMESTAMP | NO |
| score | INTEGER | NO |
| ml_score | INTEGER | YES |
| technical_score | INTEGER | YES |
| stop_score | INTEGER | YES |
| prev_score | INTEGER | YES |
| score_delta | INTEGER | YES |
| factors_json | VARCHAR | YES |
| was_pushed | BOOLEAN | NO |
| push_type | VARCHAR | YES |
| plan_type | VARCHAR | YES |
| created_at | TIMESTAMP | YES |

## market_bars_daily (5,443,762 rows)

| Column | Type | Nullable |
|--------|------|----------|
| symbol | VARCHAR | NO |
| trade_date | DATE | NO |
| open | DECIMAL(12,4) | NO |
| high | DECIMAL(12,4) | NO |
| low | DECIMAL(12,4) | NO |
| close | DECIMAL(12,4) | NO |
| volume | BIGINT | NO |
| amount | DECIMAL(20,2) | YES |
| is_trade | BOOLEAN | NO |
| limit_status | VARCHAR | YES |
| data_source | VARCHAR | NO |

## market_bars_intraday (44,115 rows)

| Column | Type | Nullable |
|--------|------|----------|
| symbol | VARCHAR | NO |
| bar_time | TIMESTAMP | NO |
| timeframe | VARCHAR | NO |
| open | DECIMAL(12,4) | NO |
| high | DECIMAL(12,4) | NO |
| low | DECIMAL(12,4) | NO |
| close | DECIMAL(12,4) | NO |
| volume | BIGINT | NO |
| amount | DECIMAL(20,2) | YES |
| is_complete | BOOLEAN | NO |
| data_source | VARCHAR | NO |

## model_performance_daily (5 rows)

| Column | Type | Nullable |
|--------|------|----------|
| model_name | VARCHAR | NO |
| trade_date | DATE | NO |
| ic | DOUBLE | YES |
| ic_ir | DOUBLE | YES |
| precision_top20 | DOUBLE | YES |
| warning_precision | DOUBLE | YES |
| miss_rate | DOUBLE | YES |
| push_count | INTEGER | YES |
| created_at | TIMESTAMP | YES |

## model_registry (95 rows)

| Column | Type | Nullable |
|--------|------|----------|
| model_id | VARCHAR | NO |
| model_type | VARCHAR | NO |
| trained_at | TIMESTAMP | YES |
| train_start | DATE | YES |
| train_end | DATE | YES |
| val_start | DATE | YES |
| val_end | DATE | YES |
| metrics_json | VARCHAR | YES |
| model_path | VARCHAR | YES |
| is_active | BOOLEAN | NO |

## moneyflow_daily (284 rows)

| Column | Type | Nullable |
|--------|------|----------|
| symbol | VARCHAR | NO |
| trade_date | DATE | NO |
| buy_sm_amount | DOUBLE | YES |
| sell_sm_amount | DOUBLE | YES |
| buy_md_amount | DOUBLE | YES |
| sell_md_amount | DOUBLE | YES |
| buy_lg_amount | DOUBLE | YES |
| sell_lg_amount | DOUBLE | YES |
| buy_elg_amount | DOUBLE | YES |
| sell_elg_amount | DOUBLE | YES |
| net_mf_amount | DOUBLE | YES |

## optuna_history (0 rows)

| Column | Type | Nullable |
|--------|------|----------|
| study_id | INTEGER | NO |
| model_type | VARCHAR | NO |
| run_date | DATE | NO |
| n_trials | INTEGER | YES |
| best_ic | DOUBLE | YES |
| prev_ic | DOUBLE | YES |
| best_params_json | VARCHAR | YES |
| promoted | BOOLEAN | NO |
| created_at | TIMESTAMP | YES |

## regime_history (0 rows)

| Column | Type | Nullable |
|--------|------|----------|
| trade_date | DATE | NO |
| is_trend | BOOLEAN | NO |
| prob_trend | DOUBLE | YES |
| regime_source | VARCHAR | YES |
| hs300_ma20_bias | DOUBLE | YES |
| mkt_up_ratio | DOUBLE | YES |
| created_at | TIMESTAMP | YES |

## signal_events (395 rows)

| Column | Type | Nullable |
|--------|------|----------|
| event_id | INTEGER | NO |
| symbol | VARCHAR | NO |
| event_time | TIMESTAMP | NO |
| event_type | VARCHAR | NO |
| severity | VARCHAR | NO |
| message | VARCHAR | YES |
| context_json | VARCHAR | YES |
| is_read | BOOLEAN | NO |

## stop_loss_log (0 rows)

| Column | Type | Nullable |
|--------|------|----------|
| log_id | INTEGER | NO |
| symbol | VARCHAR | NO |
| trigger_time | TIMESTAMP | NO |
| stop_type | VARCHAR | NO |
| entry_price | DOUBLE | YES |
| stop_price | DOUBLE | YES |
| close_at_trigger | DOUBLE | YES |
| atr_at_trigger | DOUBLE | YES |
| atr_multiplier | DOUBLE | YES |
| was_correct | BOOLEAN | YES |
| actual_ret_3d | DOUBLE | YES |
| created_at | TIMESTAMP | YES |

## system_config (2 rows)

| Column | Type | Nullable |
|--------|------|----------|
| key | VARCHAR | NO |
| value | VARCHAR | NO |
| updated_at | TIMESTAMP | YES |

## task_run_log (70 rows)

| Column | Type | Nullable |
|--------|------|----------|
| id | INTEGER | NO |
| task_id | VARCHAR | NO |
| run_time | TIMESTAMP | NO |
| status | VARCHAR | NO |
| duration_s | FLOAT | YES |
| error | VARCHAR | YES |

## trade_calendar (8,797 rows)

| Column | Type | Nullable |
|--------|------|----------|
| trade_date | DATE | NO |

## trade_log (0 rows)

| Column | Type | Nullable |
|--------|------|----------|
| trade_id | INTEGER | NO |
| symbol | VARCHAR | NO |
| trade_date | DATE | NO |
| trade_type | VARCHAR | NO |
| price | DOUBLE | NO |
| qty | INTEGER | NO |
| amount | DOUBLE | YES |
| reason | VARCHAR | YES |
| score_at_trade | INTEGER | YES |
| notes | VARCHAR | YES |
| created_at | TIMESTAMP | YES |

## train_pool (3,179 rows)

| Column | Type | Nullable |
|--------|------|----------|
| symbol | VARCHAR | NO |
| name | VARCHAR | YES |
| total_mv_yi | FLOAT | YES |
| avg_amount_wan | FLOAT | YES |
| listing_days | INTEGER | YES |
| updated_at | TIMESTAMP | YES |

## watchlist (35 rows)

| Column | Type | Nullable |
|--------|------|----------|
| id | INTEGER | NO |
| symbol | VARCHAR | NO |
| name | VARCHAR | NO |
| watch_group | VARCHAR | NO |
| position_status | BOOLEAN | NO |
| position_cost | DECIMAL(12,4) | YES |
| position_qty | INTEGER | YES |
| manual_tags | VARCHAR | YES |
| updated_at | TIMESTAMP | YES |

## weekly_reports (2 rows)

| Column | Type | Nullable |
|--------|------|----------|
| report_date | DATE | NO |
| report_md | VARCHAR | YES |
| weekly_stats_json | VARCHAR | YES |
| created_at | TIMESTAMP | YES |

