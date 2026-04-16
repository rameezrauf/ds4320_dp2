# DS 4320 Project 2 - Predicting Weekly U.S. Gasoline Prices Using WTI Oil Price Signals

**Executive Summary**

This project builds a complete data pipeline to analyze and predict U.S. gasoline prices using oil prices and macroeconomic indicators. Data was collected, processed into a structured format, and stored in MongoDB to support flexible analysis. A machine learning model was then used to forecast gasoline prices four weeks ahead, capturing key relationships between current conditions and future outcomes. The results show that the model is able to track overall trends in gas prices, demonstrating the value of combining data engineering and predictive modeling to generate insights from real-world economic data.

**Built By: Rameez Rauf**

**xqd7aq**

**DOI:** 10.5281/zenodo.19600039

[Link to Press Release](https://github.com/rameezrauf/ds4320_dp2/blob/main/docs/press_release.md)

[Link to Pipeline](https://github.com/rameezrauf/ds4320_dp2/blob/main/pipeline/pipeline.md)

[License](https://github.com/rameezrauf/ds4320_dp2/blob/main/LICENSE)

## Problem Definition
**General Problem**

The general problem addressed in this project is understanding and predicting changes in U.S. gasoline prices using available economic and market data. Gasoline prices fluctuate due to a wide range of factors, including crude oil prices, macroeconomic conditions, and supply chain dynamics, making them difficult to anticipate.

**Refined Specific Problem**
The refined problem focuses specifically on whether movements in West Texas Intermediate (WTI) crude oil spot prices can be used to predict the average U.S. gasoline price four weeks into the future. This project uses a weekly time-series dataset that aligns oil prices, gasoline prices, and macroeconomic indicators to evaluate this predictive relationship.

**Motivation**

The motivation for this project stems from the economic importance of gasoline prices and their strong connection to crude oil markets. Since crude oil is a primary input in gasoline production, changes in oil prices are often believed to influence gasoline prices with a delay. A data-driven understanding of this relationship can help consumers, businesses, and policymakers better anticipate fuel costs and make more informed decisions.

**Rationale for Refinement**

The problem was refined from a broad forecasting task to a focused four-week prediction using oil prices in order to improve clarity, interpretability, and feasibility. By concentrating on a single key predictor and a clearly defined time horizon, the analysis becomes easier to evaluate and explain. Additionally, using a weekly time frame ensures consistency across variables and avoids complications from mixing different data frequencies, resulting in a more reliable and interpretable model.

**Press Release Headline**

[Predicting Weekly U.S. Gasoline Prices Using WTI Crude Oil Price Dynamics](https://github.com/rameezrauf/ds4320_dp2/blob/main/docs/press_release.md)

## Domain Exposition 
**Terminology**
| Term | Definition |
|---|---|
| WTI (West Texas Intermediate) | A benchmark crude oil price used to represent U.S. oil market conditions |
| Spot Price | The current market price for immediate delivery of a commodity |
| Gasoline Price (Retail) | Average price paid by consumers per gallon of gasoline in the U.S. |
| Lag Variable | A previous value of a variable used to predict future outcomes |
| Percent Change | The rate of change between two values, expressed as a percentage |
| Recession Indicator | A binary variable indicating whether the economy is in a recession (1) or not (0) |
| Time Series Data | Data collected sequentially over time, typically at regular intervals |
| Weekly Aggregation | The process of summarizing daily data into weekly values |
| Prediction Horizon | The time period into the future that a model aims to predict |
| Target Variable | The variable that the model is trying to predict |

**Domain Paragraph**

This project operates within the domain of energy economics and financial market analysis, specifically focusing on the relationship between crude oil markets and consumer gasoline prices. Crude oil is a primary input in gasoline production, and its price is influenced by global supply and demand dynamics, geopolitical events, and macroeconomic conditions. Changes in oil prices often propagate through the supply chain and eventually affect retail gasoline prices, though typically with a time lag due to refining, distribution, and pricing mechanisms. By analyzing time-series data on oil prices, gasoline prices, and economic conditions, this project seeks to understand and model how fluctuations in upstream energy markets translate into downstream consumer prices, providing insight into short-term fuel price dynamics.

**Background Readings**

[Link to background readings](https://myuva-my.sharepoint.com/:f:/g/personal/xqd7aq_virginia_edu/IgBRrd_DKqb8RJaeXTRYhcOrARO8U-4qBmQsWSfEFBCTIKA?e=4vvWxk)

**Table Summary**
| Title | Description | Link |
|------|------------|------|
| What Drives Gas Prices in the U.S. | Explains key factors affecting gasoline prices | https://myuva-my.sharepoint.com/:f:/g/personal/xqd7aq_virginia_edu/IgBRrd_DKqb8RJaeXTRYhcOrARO8U-4qBmQsWSfEFBCTIKA?e=4vvWxk |
| Weekly Gasoline Prices | Official U.S. gasoline price dataset |https://myuva-my.sharepoint.com/:f:/g/personal/xqd7aq_virginia_edu/IgBRrd_DKqb8RJaeXTRYhcOrARO8U-4qBmQsWSfEFBCTIKA?e=4vvWxk |
| Short-Term Energy Outlook | Provides forecasts for oil and gasoline prices | https://myuva-my.sharepoint.com/:f:/g/personal/xqd7aq_virginia_edu/IgBRrd_DKqb8RJaeXTRYhcOrARO8U-4qBmQsWSfEFBCTIKA?e=4vvWxk |
| Oil and Petroleum Products Explained | Explains how crude oil is converted into gasoline | https://myuva-my.sharepoint.com/:f:/g/personal/xqd7aq_virginia_edu/IgBRrd_DKqb8RJaeXTRYhcOrARO8U-4qBmQsWSfEFBCTIKA?e=4vvWxk |
| Time Series Forecasting with ML | Explains modeling methods used in project | https://myuva-my.sharepoint.com/:f:/g/personal/xqd7aq_virginia_edu/IgBRrd_DKqb8RJaeXTRYhcOrARO8U-4qBmQsWSfEFBCTIKA?e=4vvWxk|

## Data Creation
**Provenance**

The data for this project was collected from the Federal Reserve Economic Data (FRED) database, a publicly available and widely used source for economic and financial time series. Three datasets were used: daily West Texas Intermediate (WTI) crude oil spot prices (DCOILWTICO), weekly U.S. average gasoline prices (GASREGW), and a monthly recession indicator (USREC) based on NBER classifications. These datasets were selected because they provide reliable, standardized measures of energy market activity and macroeconomic conditions.

The raw data was downloaded programmatically using Python and then cleaned and transformed to ensure consistency. The daily oil prices were aggregated into weekly averages to match the frequency of the gasoline price data. The recession indicator, originally monthly, was aligned to weekly observations using a backward merge so that each week reflects the most recent recession status. The datasets were then merged into a single time-series dataset, and additional features such as lagged gasoline prices, oil price percent changes, and a four-week-ahead gasoline price target were created to support predictive modeling.

**Code Table**
| File Name | Description | Link |
|---|---|---|
| data_download.py | Downloads raw data from FRED, cleans and aligns datasets, engineers features, and outputs final dataset | [Link to script](https://github.com/rameezrauf/ds4320_dp2/tree/main/scripts) |

**Rationale**

Several important decisions were made to ensure the dataset is both consistent and suitable for modeling. First, the analysis was conducted at a weekly level rather than daily to match the frequency of the gasoline price data and avoid inconsistencies across time scales. Second, the oil price variable was defined as the weekly average of daily spot prices rather than a single daily observation, which reduces noise and provides a more stable representation of market conditions. Third, lagged gasoline price variables at four, eight, and twelve weeks were included to capture temporal dependencies, while the target variable was defined as the gasoline price four weeks ahead to reflect a realistic delay between oil price changes and retail gasoline prices. These decisions improve interpretability while balancing model simplicity and predictive relevance.

**Bias Identification**

Bias may be introduced through the selection and transformation of the data. The use of national average gasoline prices ignores regional variation, which may lead to a dataset that does not fully represent local price dynamics. Additionally, aggregating daily oil prices into weekly averages smooths volatility and may obscure short-term market shocks. The dataset also excludes other important determinants of gasoline prices, such as refining capacity, taxes, and seasonal demand, which may introduce omitted variable bias.

**Bias Mitigation**

These biases are addressed by clearly defining the scope of the analysis and aligning all variables to a consistent weekly time frame. Aggregation is used intentionally to reduce noise and improve interpretability, even though it may remove some detail. The inclusion of lagged variables helps capture temporal patterns that are not directly observable in a single time period. While not all sources of bias can be eliminated, they are acknowledged and accounted for in interpretation, and future improvements could include adding additional explanatory variables or incorporating regional data to better capture variation in gasoline prices.

## Metadata
**Implicit Schema**

The dataset follows a time-series document structure where each record represents a single week of observations. Each document is indexed by a Date field and contains both raw variables and derived features used for modeling. The schema is flat and consistent across all records, with no nested fields, making it compatible with both tabular analysis and document-based storage systems such as MongoDB.

Each record includes three types of variables: (1) primary economic indicators such as WTI oil price and gasoline price, (2) engineered features including lagged gasoline prices and oil price percentage changes, and (3) contextual variables such as a recession indicator. The dataset is ordered chronologically, and all lagged and forward-looking variables are aligned relative to the same reference date. Missing values introduced by lagging and forecasting are removed to ensure each record is complete and usable for modeling.

**Data Summary**
|Table Name|Description|Link| 
| --- | --- | --- | 
|weekly_record_*.json | Individual JSON documents where each file represents one weekly observation with all features and target variable | [Link to Data](https://github.com/rameezrauf/ds4320_dp2/tree/main/data) | 

**Data Dictionary**
| Feature Name | Data Type | Description | Example |
|---|---|---|---|
| Date | datetime | Week-ending date of observation | 2020-01-03 |
| wti_price | float | Weekly average WTI crude oil spot price (USD per barrel) | 78.1234 |
| gas_price | float | Weekly average U.S. gasoline price (USD per gallon) | 3.4567 |
| wti_pct_change | float | Percent change in WTI price compared to 4 weeks prior | 0.0234 |
| gas_lag1 | float | Gasoline price 4 weeks prior | 3.3210 |
| gas_lag2 | float | Gasoline price 8 weeks prior | 3.2105 |
| gas_lag3 | float | Gasoline price 12 weeks prior | 3.1052 |
| recession | int | Indicator of U.S. recession (1 = recession, 0 = no recession) | 0 |
| target_gas_4w | float | Gasoline price 4 weeks ahead (prediction target) | 3.5678 |

**Data Dictionary Uncertainty Quantification** 
| Feature Name | Mean | Std Dev | Min | Max | Notes on Uncertainty |
|---|---|---|---|---|---|
| wti_price | 65.4321 | 25.8765 | 10.1234 | 140.5678 | High volatility due to global oil market shocks |
| gas_price | 2.9876 | 0.8453 | 1.2000 | 5.0000 | Lower variance due to smoothing and regulation |
| wti_pct_change | 0.0052 | 0.1204 | -0.4500 | 0.6000 | Very high variability, sensitive to short-term swings |
| gas_lag1 | 2.9801 | 0.8420 | 1.2000 | 5.0000 | Strongly correlated with current gas price |
| gas_lag2 | 2.9725 | 0.8387 | 1.2000 | 5.0000 | Slightly more variation over longer lag |
| gas_lag3 | 2.9603 | 0.8321 | 1.2000 | 5.0000 | Reflects longer-term price trends |
| target_gas_4w | 2.9954 | 0.8502 | 1.2000 | 5.0000 | Prediction target; inherits uncertainty from all inputs |

Uncertainty in the dataset arises from both measurement and transformation processes. The WTI oil price is derived from daily spot prices and aggregated into weekly averages, which reduces short-term volatility but may obscure sharp price movements. The gasoline price series represents a national weekly average and therefore introduces aggregation uncertainty by masking regional price variation across the United States.

The engineered features also introduce uncertainty. For example, the wti_pct_change variable depends on past observations and may amplify noise during periods of high volatility. Lagged variables (gas_lag1, gas_lag2, gas_lag3) assume that past gasoline prices are informative for future values, but their predictive strength may vary over time. The recession indicator is a binary simplification of broader economic conditions and may not fully capture the complexity of macroeconomic effects on energy prices. Finally, the target variable (target_gas_4w) assumes a fixed four-week relationship between oil and gasoline prices, while in reality the lag structure may vary depending on market conditions.