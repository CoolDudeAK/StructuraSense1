from backend.app import create_app

app = create_app(start_workers=False, seed_demo=True)
