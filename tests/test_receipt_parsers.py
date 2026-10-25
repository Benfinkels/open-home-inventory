"""
Unit tests for Merchant Receipt Parsers: Amazon, Micro Center,
Best Buy, Direct OEM, and Specialty Bike Shops.
"""

import pytest
from src.receipts.parsers.amazon import AmazonReceiptParser
from src.receipts.parsers.microcenter import MicroCenterReceiptParser
from src.receipts.parsers.bestbuy import BestBuyReceiptParser
from src.receipts.parsers.direct_oem import DirectOEMReceiptParser
from src.receipts.parsers.bike_shops import BikeShopReceiptParser
from src.receipts.parser_registry import parse_receipt_text


def test_amazon_parser():
    parser = AmazonReceiptParser()
    sender = "auto-confirm@amazon.com"
    subject = "Your Amazon.com order of Dell UltraSharp 4K"
    body = """
    Order #: 112-9842109-8472911
    Order Date: August 10, 2023

    Item(s) Ordered:
    1 of: Dell UltraSharp 27 4K USB-C Hub Monitor (U2723QE)
    Price: $579.99

    Grand Total: $616.24
    """
    assert parser.can_parse(sender, subject, body) is True
    receipt = parser.parse("msg-amz-01", "2023-08-10", sender, subject, body)
    assert receipt is not None
    assert receipt.merchant == "Amazon.com"
    assert receipt.order_number == "112-9842109-8472911"
    assert len(receipt.items) == 1
    assert "Dell UltraSharp" in receipt.items[0].name
    assert receipt.items[0].price_usd == 579.99


def test_microcenter_parser():
    parser = MicroCenterReceiptParser()
    sender = "e-receipt@microcenter.com"
    subject = "Micro Center eReceipt"
    body = """
    Store: #055 Cambridge Micro Center
    Order Number: MC-892410-ORD
    Purchase Date: 2023-11-15

    ITEMS PURCHASED:
    1. NVIDIA GEFORCE RTX 4090 FOUNDERS EDITION 24GB GDDR6X
       SKU: 489218
       Serial Number: SN-RTX4090-FE-2024-8842
       Price: $1,599.99

    TOTAL: $1,699.98
    """
    assert parser.can_parse(sender, subject, body) is True
    receipt = parser.parse("msg-mc-01", "2023-11-15", sender, subject, body)
    assert receipt is not None
    assert receipt.merchant == "Micro Center"
    assert receipt.order_number == "MC-892410-ORD"
    assert len(receipt.items) == 1
    assert "RTX 4090" in receipt.items[0].name
    assert receipt.items[0].serial_number == "SN-RTX4090-FE-2024-8842"
    assert receipt.items[0].price_usd == 1599.99


def test_bike_shop_parser():
    parser = BikeShopReceiptParser()
    sender = "orders@wheelworks.com"
    subject = "Wheelworks Cycling Pro Shop Invoice"
    body = """
    Invoice Number: WW-2023-4921
    Date: April 20, 2023

    Item: Specialized Tarmac SL7 Expert Di2 Road Bicycle
    Frame Serial Number: WSBC604291884T
    Unit Price: $6,500.00
    Total Paid: $6,906.25
    """
    assert parser.can_parse(sender, subject, body) is True
    receipt = parser.parse("msg-ww-01", "2023-04-20", sender, subject, body)
    assert receipt is not None
    assert receipt.merchant == "Wheelworks Cycling"
    assert receipt.order_number == "WW-2023-4921"
    assert len(receipt.items) == 1
    assert "Specialized Tarmac" in receipt.items[0].name
    assert receipt.items[0].serial_number == "WSBC604291884T"
    assert receipt.items[0].price_usd == 6500.00


def test_parse_receipt_text_convenience():
    raw_text = """
    Store: #055 Cambridge Micro Center
    Order Number: MC-990011
    Purchase Date: 2024-02-01
    1. Intel Core i9-14900K Processor
       SKU: 618291
       Price: $549.99
    TOTAL: $549.99
    """
    receipt = parse_receipt_text(raw_text)
    assert receipt is not None
    assert receipt.merchant == "Micro Center"
    assert len(receipt.items) == 1
    assert "Core i9-14900K" in receipt.items[0].name
