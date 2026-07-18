from pwa import main
import asyncio

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[AutoBridge] Stopped by user.")
