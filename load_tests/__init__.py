"""KiX platform load testing infrastructure.

Goal: find the system's actual breaking point at 10K+ concurrent merchants.
Built on Locust. Mock mode (no real Stripe/FCM). Reads seed data from
``load_tests/data/``.
"""
