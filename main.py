import asyncio

from rewire import DependenciesModule, LifecycleModule, LoaderModule, Space


async def main() -> None:
    async with Space().init().use():
        import rewire_fastapi
        import rewire_sqlmodel.ext.fastapi

        await LoaderModule.get().discover().load()
        await DependenciesModule.get().add(
            rewire_sqlmodel.plugin,
            rewire_fastapi.plugin,
            rewire_sqlmodel.ext.fastapi.plugin,
        ).solve()
        await LifecycleModule.get().start()


if __name__ == "__main__":
    asyncio.run(main())
