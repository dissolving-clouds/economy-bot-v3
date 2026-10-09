"""Магазин ролей: роли сервера (навсегда) и роли игроков (на ограниченный срок)."""
import logging
import os
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from cogs.custom_rooms import BuyRoomModal
from utils import mark_self_delete, TempView, add_role, calc_fee, remove_role, role_is_manageable, send

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- покупки

async def buy_server_role(bot, interaction: discord.Interaction, role_id: int):
    """Покупка роли из магазина администратора (выдаётся навсегда)."""
    db, guild, member = bot.db, interaction.guild, interaction.user

    async def say(text):
        await send(interaction, text)

    role = guild.get_role(role_id)
    item = await db.get_shop_role(role_id)
    if role is None or item is None:
        return await say("❌ Эта роль больше не продаётся.")
    if await db.get_item(member.id, role_id) or role in member.roles:
        return await say("✅ У тебя уже есть эта роль.")
    if not role_is_manageable(guild, role):
        return await say("❌ Я не могу выдать эту роль: она выше моей. Подними роль бота выше в настройках сервера.")

    price = item["price"]
    if not await db.try_remove(member.id, price):
        balance = await db.get_balance(member.id)
        return await say(f"❌ Не хватает монет. Цена: {await db.fmt(price)}, у тебя: {await db.fmt(balance)}.")
    if not await add_role(guild, member.id, role, "Покупка в магазине"):
        await db.add_balance(member.id, price)
        return await say("❌ Не удалось выдать роль, монеты возвращены.")

    await db.add_item(member.id, role_id, 0, 1)
    await say(f"🎉 {member.mention} купил роль {role.mention} за {await db.fmt(price)}! Она лежит в /inventory.")


async def buy_player_role(bot, interaction: discord.Interaction, role_id: int):
    """Покупка роли, созданной другим игроком."""
    db, guild, member = bot.db, interaction.guild, interaction.user

    async def say(text):
        await send(interaction, text)

    role = guild.get_role(role_id)
    cr = await db.get_custom_role(role_id)
    now = int(time.time())
    if role is None or cr is None or not cr["listed"]:
        return await say("❌ Эта роль больше не продаётся.")
    if cr["creator_id"] == member.id:
        return await say("❌ Это твоя собственная роль.")
    if await db.get_item(member.id, role_id):
        return await say("✅ Эта роль уже есть в твоём инвентаре. Продлить её можно в /inventory.")
    if cr["expires_at"] - now < 86400:
        return await say("❌ Лот скоро закончится (автор не продлил роль), покупка сейчас недоступна.")

    price = cr["price"]
    if not await db.try_remove(member.id, price):
        balance = await db.get_balance(member.id)
        return await say(f"❌ Не хватает монет. Цена: {await db.fmt(price)}, у тебя: {await db.fmt(balance)}.")
    if not await add_role(guild, member.id, role, "Покупка роли игрока"):
        await db.add_balance(member.id, price)
        return await say("❌ Не удалось выдать роль, монеты возвращены.")

    days = await db.get_int("user_role_days")
    percent = await db.get_int("role_commission")
    fee = calc_fee(price, percent)
    await db.add_balance(cr["creator_id"], price - fee)
    await db.add_transaction(now, member.id, cr["creator_id"], price, fee, "role_buy", f"роль {role.name}")
    await db.add_role_sale(now, role_id, member.id, price - fee, "buy")
    await db.add_item(member.id, role_id, now + days * 86400, 1)
    await say(
        f"🎉 {member.mention} купил роль {role.mention} на {days} дн. за {await db.fmt(price)}! "
        f"<@{cr['creator_id']}> получает **{await db.fmt(price - fee)}** за продажу (комиссия {percent}%). "
        f"Продлевать роль нужно в /inventory."
    )


# ---------------------------------------------------------------- создание роли игрока

def parse_colour(raw: str | None) -> discord.Colour | None:
    """'#ff66aa' / 'ff66aa' / '' -> Colour. None — если формат неверный."""
    if not raw or not raw.strip():
        return discord.Colour.default()
    raw = raw.strip()
    if re.fullmatch(r"[0-9a-fA-F]{6}", raw):
        raw = "#" + raw
    try:
        return discord.Colour.from_str(raw)
    except ValueError:
        return None


async def create_player_role(bot, interaction: discord.Interaction, name: str, colour: discord.Colour, price: int):
    """Создаёт роль игрока и выставляет её в /shop. Вызывается после подтверждения."""
    db, guild, member = bot.db, interaction.guild, interaction.user
    limit = await db.get_int("user_role_limit")
    if await db.count_custom_roles_of(member.id) >= limit:
        return await send(interaction, f"❌ Можно создать не больше {limit} ролей.")
    if not await db.try_remove(member.id, price):
        return await send(interaction, f"❌ Не хватает монет. Создание роли стоит {await db.fmt(price)} (равно цене продажи).")
    try:
        role = await guild.create_role(name=name, colour=colour, reason=f"Роль игрока {member}")
    except discord.HTTPException:
        await db.add_balance(member.id, price)
        return await send(interaction, "❌ Не удалось создать роль (проверь права бота). Монеты возвращены.")

    days = await db.get_int("user_role_days")
    expires = int(time.time()) + days * 86400
    await db.add_custom_role(role.id, member.id, price, expires)
    await db.add_item(member.id, role.id, expires, 1)
    await add_role(guild, member.id, role, "Создатель роли")

    embed = discord.Embed(title="🎨 Новая роль в магазине!", color=colour)
    embed.description = f"{member.mention} создал(а) роль {role.mention} и выставил(а) её на продажу"
    embed.add_field(name="Цена", value=await db.fmt(price))
    embed.add_field(name="Действует до", value=f"<t:{expires}:D>")
    embed.set_footer(text=f"Продлевать роль нужно каждые {days} дн. в /inventory, иначе она удалится.")
    await send(interaction, embed=embed)


class RoleCreateModal(discord.ui.Modal, title="Создать свою роль"):
    role_name = discord.ui.TextInput(label="Название роли", max_length=32, placeholder="например: Звёздная пыль")
    color = discord.ui.TextInput(label="Цвет #RRGGBB (необязательно)", required=False, max_length=7,
                                 placeholder="#ff66aa")
    price = discord.ui.TextInput(label=f"Цена продажи (от {config.ROLE_MIN_PRICE})", max_length=12,
                                 default=str(config.ROLE_MIN_PRICE))

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        db = self.bot.db
        name = " ".join(self.role_name.value.split())
        if not name:
            return await interaction.response.send_message("❌ Название не может быть пустым.", ephemeral=True)
        colour = parse_colour(self.color.value)
        if colour is None:
            return await interaction.response.send_message("❌ Цвет нужно указать так: #ff66aa", ephemeral=True)
        try:
            price = int(self.price.value.strip().replace(" ", ""))
        except ValueError:
            return await interaction.response.send_message("❌ Цена должна быть целым числом.", ephemeral=True)
        if not config.ROLE_MIN_PRICE <= price <= config.ROLE_PRICE_CAP:
            return await interaction.response.send_message(
                f"❌ Цена должна быть от {config.ROLE_MIN_PRICE} до {config.ROLE_PRICE_CAP}.", ephemeral=True)
        days = await db.get_int("user_role_days")
        percent = await db.get_int("role_commission")
        embed = discord.Embed(title="🎨 Проверь свою роль", color=colour)
        embed.add_field(name="Название", value=name)
        embed.add_field(name="Цвет", value=f"#{colour.value:06x}" if colour.value else "без цвета")
        embed.add_field(name="Цена продажи", value=await db.fmt(price))
        embed.description = (
            f"Создание стоит столько же, сколько цена: **{await db.fmt(price)}**.\n"
            f"Роль живёт {days} дн., продлевать — в /inventory. С каждой продажи ты получаешь цену минус {percent}%."
        )
        await interaction.response.send_message(embed=embed, view=RoleCreateConfirm(self.bot, name, colour, price),
                                                ephemeral=True)


class RoleCreateConfirm(discord.ui.View):
    def __init__(self, bot, name, colour, price):
        super().__init__(timeout=120)
        self.bot, self.name, self.colour, self.price = bot, name, colour, price
        self.used = False

    @discord.ui.button(label="Создать и выставить на продажу", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.used:
            return
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="⏳ Создаю роль…", embed=None, view=None)
        await create_player_role(self.bot, interaction, self.name, self.colour, self.price)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Отменено.", embed=None, view=None)


# ---------------------------------------------------------------- интерфейс

class ShopSelect(discord.ui.Select):
    def __init__(self, bot, options):
        super().__init__(placeholder="Выбрать", options=options, row=0)
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "croom":  # кастомная голосовая комната — сначала спрашиваем название
            if await self.bot.db.get_croom(interaction.user.id):
                return await send(interaction, "✅ У тебя уже есть голосовая комната. Управлять ей — командой /room.")
            return await interaction.response.send_modal(BuyRoomModal(self.bot))
        await interaction.response.defer()  # меню остаётся, результат придёт отдельным сообщением
        kind, role_id = self.values[0].split(":")
        if kind == "s":
            await buy_server_role(self.bot, interaction, int(role_id))
        else:
            await buy_player_role(self.bot, interaction, int(role_id))


class ShopView(TempView):
    def __init__(self, bot, options):
        super().__init__(timeout=config.MENU_LIFETIME)  # меню исчезает после паузы в нажатиях
        self.bot = bot
        self.add_item(ShopSelect(bot, options))

    @discord.ui.button(label="Создать свою роль", emoji="🎨", style=discord.ButtonStyle.success, row=1)
    async def create_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RoleCreateModal(self.bot))


def shop_banner() -> discord.File | None:
    path = os.path.join(BASE_DIR, config.PICS_DIR, config.SHOP_IMAGE)
    if not os.path.isfile(path):
        return None
    return discord.File(path, filename=config.SHOP_IMAGE)


class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.expire_roles.start()

    async def cog_unload(self):
        self.expire_roles.cancel()

    # -------- /shop
    @app_commands.command(name="shop", description="Магазин: роли, голосовые комнаты, создание своей роли")
    @app_commands.guild_only()
    async def shop(self, interaction: discord.Interaction):
        db = self.bot.db
        guild = interaction.guild
        now = int(time.time())
        server_lines, player_lines, options = [], [], []

        # кастомная голосовая комната — всегда первой строкой
        croom_price = await db.get_int("croom_price")
        croom_days = await db.get_int("croom_days")
        croom_term = f"на {croom_days} дн." if croom_days > 0 else "навсегда"
        options.append(discord.SelectOption(
            label="Кастомная голосовая комната", emoji="🔊", value="croom",
            description=f"{croom_price} · {croom_term} · управление через /room"[:100],
        ))

        for item in await db.get_shop_roles():
            role = guild.get_role(item["role_id"])
            if role is None:
                continue
            line = f"{role.mention} — **{await db.fmt(item['price'])}**"
            if item["description"]:
                line += f"\n└ {item['description']}"
            server_lines.append(line)
            if len(options) < 25:
                desc = f"{item['price']} · {item['description']}"[:100]
                options.append(discord.SelectOption(label=role.name[:100], description=desc, value=f"s:{role.id}"))

        days = await db.get_int("user_role_days")
        for cr in await db.get_listed_custom_roles(now):
            role = guild.get_role(cr["role_id"])
            if role is None:
                continue
            buyers = (await db.role_sale_stats(cr["role_id"], cr["creator_id"]))["buyers"]
            player_lines.append(
                f"{role.mention} — **{await db.fmt(cr['price'])}** на {days} дн. · 🛒 купили: **{buyers}**\n"
                f"└ Автор: <@{cr['creator_id']}> · лот до <t:{cr['expires_at']}:D>"
            )
            if len(options) < 25:
                options.append(discord.SelectOption(
                    label=role.name[:100], value=f"p:{role.id}",
                    description=f"{cr['price']} · роль игрока · купили {buyers}"[:100],
                ))

        embed = discord.Embed(title="🏪 Магазин", color=discord.Color.blurple())
        embed.add_field(
            name="🔊 Кастомная голосовая комната",
            value=(f"**{await db.fmt(croom_price)}** · {croom_term}\n"
                   f"└ Своё название и цвет роли, приглашай друзей через /room"),
            inline=False,
        )
        if server_lines:
            embed.add_field(name="🏛 Роли сервера (навсегда)", value="\n\n".join(server_lines)[:1024], inline=False)
        if player_lines:
            embed.add_field(name="🎨 Роли игроков", value="\n\n".join(player_lines)[:1024], inline=False)
        else:
            embed.add_field(name="🎨 Роли игроков", value="Пока никто не выставил роль — стань первым!", inline=False)
        embed.add_field(
            name="✨ Своя роль",
            value=f"Кнопка «Создать свою роль»: название, цвет и цена (от {config.ROLE_MIN_PRICE}). "
                  f"Ты получаешь деньги с каждой продажи.",
            inline=False,
        )
        embed.set_footer(text=f"Твой баланс: {await db.get_balance(interaction.user.id)} · "
                              f"выбери товар в меню «Выбрать»")

        view = ShopView(self.bot, options)
        banner = shop_banner()
        if banner:
            top = discord.Embed(color=0x2B2D31)
            top.set_image(url=f"attachment://{config.SHOP_IMAGE}")
            await interaction.response.send_message(embeds=[top, embed], file=banner, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    # -------- Автоматическое удаление просроченных ролей
    @tasks.loop(minutes=5)
    async def expire_roles(self):
        try:
            await self._expire_roles()
        except Exception:
            logging.exception("Ошибка при удалении просроченных ролей")

    async def _expire_roles(self):
        guild = self.bot.main_guild()
        if guild is None:
            return
        db = self.bot.db
        now = int(time.time())

        # 1) роли игроков, которые автор не продлил — удаляем совсем
        for cr in await db.get_expired_custom_roles(now):
            role = guild.get_role(cr["role_id"])
            if role:
                try:
                    mark_self_delete(role.id)
                    await role.delete(reason="Срок роли игрока истёк")
                except discord.HTTPException:
                    logging.warning("Не удалось удалить роль %s, попробую позже", cr["role_id"])
                    continue
            await db.delete_custom_role(cr["role_id"])

        # 2) роли, срок которых у конкретного владельца закончился
        for it in await db.get_expired_items(now):
            role = guild.get_role(it["role_id"])
            if not await remove_role(guild, it["user_id"], role, "Срок роли истёк"):
                continue
            await db.remove_item(it["user_id"], it["role_id"])

        # 3) роли, которые кто-то удалил вручную в Discord
        for cr in await db.all_custom_roles():
            if guild.get_role(cr["role_id"]) is None:
                await db.delete_custom_role(cr["role_id"])

    @expire_roles.before_loop
    async def before_expire(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Shop(bot))
