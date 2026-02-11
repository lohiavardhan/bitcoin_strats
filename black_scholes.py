import ccxt
import numpy as np
from scipy.stats import norm
import pandas as pd

# Define your API key and secret
api_key = 'your_api_key'
api_secret = 'your_api_secret'

# Function to fetch the current Bitcoin spot price from Binance
def get_current_bitcoin_spot_price():
    exchange = ccxt.binance({
        'apiKey': api_key,
        'secret': api_secret
    })  # Initialize with API key and secret
    ticker = exchange.fetch_ticker('BTC/USDT')  # Fetch Bitcoin/USDT pair for spot price
    return ticker['last']  # Return the last price

# Function to fetch the current Bitcoin perpetual contract price from Binance Futures
def get_current_bitcoin_perpetual_price():
    exchange = ccxt.binance({
        'apiKey': api_key,
        'secret': api_secret
    })  # Initialize with API key and secret
    ticker = exchange.futures_ticker('BTCUSDT')  # Fetch Bitcoin perpetual contract price
    return ticker['last']  # Return the last price of perpetual contract

# Function to calculate Bitcoin volatility from historical data (30-day rolling volatility)
def get_bitcoin_volatility():
    exchange = ccxt.binance({
        'apiKey': api_key,
        'secret': api_secret
    })  # Initialize with API key and secret
    ohlcv = exchange.fetch_ohlcv('BTC/USDT', '1d')  # 1-day candlestick data for spot price
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    
    # Calculate daily returns
    df['returns'] = df['close'].pct_change()
    
    # Calculate 30-day rolling standard deviation (volatility)
    volatility = df['returns'].rolling(window=30).std().iloc[-1]  # Latest 30-day volatility
    return volatility * np.sqrt(365)  # Annualize volatility

# Function to fetch the real-time funding rate from Binance Futures for perpetual contracts
def get_funding_rate():
    exchange = ccxt.binance({
        'apiKey': api_key,
        'secret': api_secret
    })  # Initialize with API key and secret
    # Fetch funding rate for BTC perpetual futures (every 8 hours funding interval)
    funding_info = exchange.futures_public_get('/fapi/v1/fundingRate', {'symbol': 'BTCUSDT', 'limit': 1})
    return float(funding_info[0]['fundingRate'])  # Return the latest funding rate

# Black-Scholes formula for a perpetual call option on a cryptocurrency
def perpetual_call_price(S, K, r, sigma, q, T):
    """
    Parameters:
    S: Current price of Bitcoin (underlying asset)
    K: Strike price of the perpetual contract
    r: Risk-free rate (annualized)
    sigma: Volatility of Bitcoin (annualized)
    q: Funding rate (annualized, can be positive or negative)
    T: Time to expiration (for perpetual, we use a finite period for approximation, e.g., 1 day = 1/365 years)
    
    Returns:
    C: Perpetual call option price
    """
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    
    C = S * np.exp(-q * T) * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return C

# Fetch spot and perpetual prices from Binance
spot_price = get_current_bitcoin_spot_price()  # Current Bitcoin spot price
perpetual_price = get_current_bitcoin_perpetual_price()  # Current Bitcoin perpetual contract price

# Fetch real-time funding rate from Binance
funding_rate = get_funding_rate()  # Real-time funding rate for perpetual contracts

# Print the spot and perpetual prices to check the difference
print(f"Current Bitcoin Spot Price: ${spot_price:.2f}")
print(f"Current Bitcoin Perpetual Price: ${perpetual_price:.2f}")
print(f"Real-time Funding Rate: {funding_rate*100:.2f}%")
print(f"Price Difference: ${spot_price - perpetual_price:.2f}")

# Parameters for Black-Scholes model
K = spot_price  # You could set a different strike price if needed
r = 0.01  # Risk-free rate (1%)
sigma = get_bitcoin_volatility()  # Get the volatility from Binance
q = funding_rate  # Use the dynamic funding rate from Binance
T = 1 / 365  # Time to expiration (1 day)

# Calculate the perpetual call price
call_price = perpetual_call_price(spot_price, K, r, sigma, q, T)
print(f"Perpetual Call Option Price: ${call_price:.2f}")
