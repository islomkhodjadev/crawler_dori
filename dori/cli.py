import argparse
import asyncio
import json
from pathlib import Path
from dotenv import load_dotenv


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description='Dori pharmacy price comparison')
    sub = parser.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8000)
    session = sub.add_parser('browser-session', help='Open a visible browser to establish a permitted session')
    session.add_argument('--site', choices=['oson','arzon'], default='oson')
    session.add_argument('--profile', default='browser-profile')
    imp = sub.add_parser('import')
    imp.add_argument('file', type=Path)
    batch = sub.add_parser('crawl')
    batch.add_argument('--limit', type=int, default=5)
    batch.add_argument('--ids', help='Comma-separated local product IDs')
    batch.add_argument('--sites', default='arzon,oson')
    report = sub.add_parser('report')
    report.add_argument('--run-id', type=int)
    args = parser.parse_args()
    if args.command == 'serve':
        import uvicorn
        uvicorn.run('dori.app:create_app', factory=True, host=args.host, port=args.port)
        return
    if args.command == 'browser-session':
        from .session import open_session
        asyncio.run(open_session(args.site, args.profile))
        return
    from .storage import Store
    store = Store()
    if args.command == 'import':
        from .importer import read_products
        products = read_products(args.file.read_bytes(), args.file.name)
        ids = store.import_products(products, args.file.name)
        print(json.dumps({'imported': len(ids), 'product_ids': ids}))
    elif args.command == 'crawl':
        from .service import crawl
        if args.limit < 1:
            parser.error('--limit must be positive')
        sites = tuple(dict.fromkeys(args.sites.split(',')))
        if not set(sites) <= {'arzon', 'oson'} or not sites:
            parser.error('Use --sites arzon,oson')
        try:
            ids = [int(v) for v in args.ids.split(',')] if args.ids else None
        except ValueError:
            parser.error('--ids must be comma-separated integers')
        products = store.products(ids)[:args.limit]
        if not products:
            parser.error('Import products first')
        run_id = store.create_run(products, sites=sites)
        asyncio.run(crawl(store, run_id, products, sites))
        print(json.dumps(store.report(run_id), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(store.report(args.run_id), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
