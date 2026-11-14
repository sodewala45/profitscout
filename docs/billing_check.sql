-- Read-only. "I paid but I'm still Free": paste in Supabase SQL editor, replace the email.
-- 1. users row(s) for the address, any letter case (the API matches the lowercased address exactly)
select user_email, plan from public.users where lower(btrim(user_email)) = lower('someone@example.com');
-- 2. subscriptions the webhook recorded (variant_id must be in LEMON_PRO_VARIANT_IDS / LEMONSQUEEZY_PRODUCT_ID)
select * from public.subscriptions where user_email = lower('someone@example.com') order by updated_at desc;
-- 3. credit row (plan / credits_total should follow users.plan: pro = 50, ultimate = 120)
select * from public.user_credits where user_email = lower('someone@example.com');
-- 4. latest webhook deliveries that were processed (none at all = webhook URL / secret / events not set up)
select * from public.webhook_events order by id desc limit 30;
