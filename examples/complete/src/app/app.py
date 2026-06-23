def lambda_handler(event, context):
    """Minimal handler - returns a static greeting."""
    return {"statusCode": 200, "body": "hello from repro-lambda"}
