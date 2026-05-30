### KiX Platform — Thai catalog

### Auto-translated via OpenRouter (DeepSeek) 2026-05-31

### Source: app/i18n/catalogs/en-SG/main.ftl

###

welcome-message = ยินดีต้อนรับ { $name }!
    .description = คุณมี { $count ->
        [one] 1 ข้อความ
        *[other] { $count } ข้อความ
    }

tutorials-module-progression = ความก้าวหน้า

tutorials-module-currency = สกุลเงิน

tutorials-module-item = ไอเทม

tutorials-module-achievement = ความสำเร็จ

tutorials-module-quest = ภารกิจ

tutorials-module-tier = ระดับ

tutorials-module-event = อีเวนต์

tutorials-module-roulette = รางวัลรูเล็ต

tutorials-module-league = ลีก

tutorials-module-pass = Battle Pass

tutorials-module-smartquests = ภารกิจอัจฉริยะ

tutorials-module-storyquest = ภารกิจเรื่องราว

tutorials-module-lives = ชีวิต

tutorials-module-tourney = การแข่งขัน

tutorials-module-collection = คอลเลกชัน

tutorials-module-badgewall = กำแพงตราสัญลักษณ์

tutorials-module-streak = สตรีค

tutorials-module-voucher_builder = เครื่องมือสร้างวอเชอร์

tutorials-module-voucher = วอเชอร์

tutorials-module-social_graph = กราฟโซเชียล

tutorials-module-social_feed = ฟีดโซเชียล

tutorials-module-auto_share = แชร์อัตโนมัติ

tutorials-module-share_to_win = แชร์เพื่อชนะ

tutorials-module-energy_invite = เชิญเพื่อรับพลังงาน

tutorials-module-friend_challenge = ท้าทายเพื่อน

tutorials-module-ladder_climb = ปีนบันได

tutorials-module-streak_rescue = ช่วยเหลือสตรีค

tutorials-module-leaderboard = ตารางผู้นำ

tutorials-module-network_effect = ผลกระทบเครือข่าย

tutorials-module-score_to_coupon = คะแนน → คูปอง

tutorials-module-energy = พลังงาน

tutorials-module-upsell = ขายเพิ่ม

tutorials-module-redemption_store = ร้านแลกของ

tutorials-module-rate_limit = จำกัดอัตรา

tutorials-module-group_actions = การกระทำกลุ่ม

tutorials-module-groupbuy = ซื้อกลุ่ม

tutorials-module-atomic_group = กลุ่มอะตอมมิก

tutorials-module-pricecut = ลดราคา

tutorials-module-coop_quest = ภารกิจร่วมมือ

tutorials-module-raid = Raid

tutorials-module-squad = Squad

tutorials-module-territory = Territory

tutorials-module-gift_sending = การส่งของขวัญ

tutorials-module-trading_post = ตลาดซื้อขาย

tutorials-module-group_reward = รางวัลกลุ่ม

tutorials-module-fcfs = มาก่อนได้ก่อน

tutorials-module-limited_drop = ของรางวัลจำกัด

tutorials-module-triggers = ทริกเกอร์

tutorials-step-intro = เราจะแนะนำคุณในการตั้งค่า "{ $recipe_name }" { $module_count ->
        [one] 1 โมดูล
        *[other] { $module_count } โมดูล
    } และ { $rule_count ->
        [one] 1 กฎ
        *[other] { $rule_count } กฎ
    }

tutorials-step-navigate-engagement = คลิก Engagement ในแถบด้านข้างเพื่อเปิดตลาดโมดูล

tutorials-step-navigate-vouchers = เปิด Vouchers ในแถบด้านข้างเพื่อกำหนดค่าเทมเพลตวูเชอร์

tutorials-step-navigate-rules = เปิด Rules ในแถบด้านข้างเพื่อกำหนดกฎเหตุการณ์

tutorials-step-enable-module = เปิดใช้งานโมดูล { $module_name }

tutorials-step-configure-module = กำหนดค่า { $module_name }: { $params_summary }

tutorials-step-create-voucher-template = สร้างเทมเพลตวูเชอร์: { $template_summary }

tutorials-step-create-rule = สร้างกฎ: เมื่อ { $trigger_event } → { $actions_summary }

tutorials-step-test-action = ลองจำลอง "{ $event_name }" เพื่อทดสอบกฎ

tutorials-step-celebrate = เสร็จสิ้น! การตั้งค่า "{ $recipe_name }" ของคุณพร้อมใช้งานแล้ว

conditions-blocker-supply_exhausted = ของรางวัลในแคมเปญนี้ถูกเรียกรับหมดแล้ว

conditions-blocker-budget_exhausted = งบประมาณของแคมเปญนี้ถูกใช้หมดแล้ว

conditions-blocker-tier_required = ต้องการระดับที่สูงขึ้นสำหรับแคมเปญนี้

conditions-blocker-first_time_only = แคมเปญนี้สำหรับผู้เข้าร่วมครั้งแรกเท่านั้น

conditions-blocker-user_segment_excluded = คุณไม่อยู่ในกลุ่มผู้ใช้ที่เหมาะสม

conditions-blocker-user_segment_not_included = คุณไม่อยู่ในกลุ่มผู้ใช้ที่เหมาะสม

conditions-blocker-min_account_age_days = บัญชีของคุณใหม่เกินไปที่จะเข้าร่วมได้ในขณะนี้

conditions-blocker-user_attribute_filter = บัญชีของคุณไม่ตรงกับคุณสมบัติที่กำหนด

conditions-blocker-frequency_per_user_per_day = คุณใช้สิทธิ์ครบตามจำนวนที่กำหนดสำหรับวันนี้แล้ว กรุณาลองใหม่ในวันพรุ่งนี้

conditions-blocker-frequency_per_user_per_week = คุณใช้สิทธิ์ครบตามจำนวนที่กำหนดสำหรับสัปดาห์นี้แล้ว

conditions-blocker-frequency_per_user_per_month = คุณใช้สิทธิ์ครบตามจำนวนที่กำหนดสำหรับเดือนนี้แล้ว

conditions-blocker-frequency_per_user_total = คุณใช้สิทธิ์ครบตามจำนวนที่กำหนดทั้งหมดสำหรับแคมเปญนี้แล้ว

conditions-blocker-frequency_global_per_day = ใช้สิทธิ์ครบตามจำนวนที่กำหนดสำหรับวันนี้ทั่วระบบแล้ว

conditions-blocker-time_not_yet_started = แคมเปญยังไม่เริ่มต้น

conditions-blocker-time_already_ended = แคมเปญสิ้นสุดแล้ว

conditions-blocker-time_invalid_day_of_week = แคมเปญไม่เปิดให้บริการในวันนี้

conditions-blocker-time_invalid_hour = แคมเปญไม่เปิดให้บริการในเวลานี้

conditions-blocker-action_prerequisites_unmet = ยังไม่ได้ดำเนินการตามเงื่อนไขเบื้องต้น

conditions-blocker-campaign_not_found = ไม่พบแคมเปญ

conditions-blocker-reservation_not_found = ไม่พบการจองหรือหมดอายุแล้ว

conditions-blocker-reservation_already_committed = การจองได้ถูกยืนยันแล้ว

conditions-blocker-reservation_already_refunded = การจองได้รับการคืนเงินแล้ว

conditions-blocker-reservation_expired = การจองหมดอายุแล้ว กรุณาลองใหม่

conditions-blocker-commit_contention = มีการขัดแย้งสูงในการยืนยัน กรุณาลองใหม่

welcome_kit-item-table_stand-title = ขาตั้งโต๊ะ (A5, สองด้าน)

welcome_kit-item-table_stand-desc = สแตนดี้ตั้งโต๊ะขนาด A5 พร้อม QR code เรียกร้องให้ดำเนินการทั้งสองด้าน

welcome_kit-item-counter_standing-title = สแตนดี้เคาน์เตอร์ (A4)

welcome_kit-item-counter_standing-desc = จอแสดงผลตั้งตรงขนาด A4 สำหรับเคาน์เตอร์หรือพื้นที่ต้อนรับ

welcome_kit-item-door_sticker-title = สติกเกอร์ประตู (กลม 150 มม.)

welcome_kit-item-door_sticker-desc = สติกเกอร์ติดประตู/หน้าต่างแบบสแตติกเชิญชวนให้ผู้คนสแกน

welcome_kit-item-social_poster-title = โปสเตอร์โซเชียล (1080×1080)

welcome_kit-item-social_poster-desc = โปสเตอร์สี่เหลี่ยมพร้อมสำหรับ Instagram, Facebook, TikTok

welcome_kit-item-handover_kit-title = ชุดส่งมอบแบบเต็ม

welcome_kit-item-handover_kit-desc = สื่อทั้งหมดข้างต้นรวมเป็นไฟล์ HTML index เดียว

welcome_kit-default-tagline = สแกนเพื่อเล่น รับรางวัล

recipe_generator-match-found = พบสูตร '{ $recipe_name }' จากคลัง

recipe_generator-match-score = คะแนนความเข้ากัน { $score }; เหตุผล: { $reasons }

recipe_generator-summary-untitled = ไม่มีชื่อ

recipe_generator-summary-empty-modules = ไม่มี

recipe_generator-summary-recipe-includes = สูตร '{ $recipe_name }' ประกอบด้วย { $module_count ->
        [one] 1 โมดูล
        *[other] { $module_count } โมดูล
    }: { $module_list }, เชื่อมต่อด้วย { $rule_count ->
        [one] 1 กฎ
        *[other] { $rule_count } กฎ
    }

recipe_generator-heuristic-fallback = (เทมเพลตฮิวริสติก) จับคู่โมดูลที่เกี่ยวข้องและกฎเริ่มต้นจากคำหลัก

recipe_generator-default-description = เชิญเพื่อน 10 คน รับวอเชอร์กาแฟฟรี

modules-status-active = เปิดใช้งาน

modules-status-inactive = ปิดใช้งาน

modules-status-coming_soon = เร็วๆ นี้

modules-action-enable = เปิดใช้งาน

modules-action-disable = ปิดใช้งาน

modules-action-configure = กำหนดค่า

error-internal = เกิดข้อผิดพลาดภายใน กรุณาลองใหม่อีกครั้งในภายหลัง

error-not_found = ไม่พบทรัพยากรที่ร้องขอ

error-unauthorized = จำเป็นต้องยืนยันตัวตน

error-forbidden = คุณไม่มีสิทธิ์ในการดำเนินการนี้

error-validation = การตรวจสอบข้อมูลที่ส่งล้มเหลว

error-rate_limited = คุณทำคำขอเกินจำนวนที่กำหนด กรุณาลองใหม่ในภายหลัง

error-conflict = คำขอนี้ขัดแย้งกับสถานะทรัพยากรปัจจุบัน

common-cta-login = เข้าสู่ระบบ

common-cta-logout = ออกจากระบบ

common-cta-signup = สมัครสมาชิก

common-cta-cancel = ยกเลิก

common-cta-save = บันทึก

common-cta-confirm = ยืนยัน

common-cta-back = ย้อนกลับ

common-cta-next = ถัดไป

common-cta-loading = กำลังโหลด…

common-nav-home = หน้าแรก

common-nav-portal = พอร์ทัล

common-nav-storefront = หน้าร้าน

common-nav-play = เล่น

common-nav-connect = เชื่อมต่อ

common-currency-sgd = ดอลลาร์สิงคโปร์

common-currency-cny = หยวนจีน

common-currency-usd = ดอลลาร์สหรัฐ
