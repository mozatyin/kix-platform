### KiX Platform — Arabic (Egypt) catalog

### Auto-translated via OpenRouter (DeepSeek) 2026-05-31

### Source: app/i18n/catalogs/en-SG/main.ftl

###

welcome-message = مرحبًا { $name }!
    .description = لديك { $count ->
        [one] رسالة واحدة
        *[other] { $count } رسالة
    }

tutorials-module-progression = التقدم

tutorials-module-currency = العملة

tutorials-module-item = العنصر

tutorials-module-achievement = الإنجاز

tutorials-module-quest = المهمة

tutorials-module-tier = المستوى

tutorials-module-event = الحدث

tutorials-module-roulette = روليت المكافآت

tutorials-module-league = الدوري

tutorials-module-pass = بطاقة المعركة

tutorials-module-smartquests = المهام الذكية

tutorials-module-storyquest = مهمة القصة

tutorials-module-lives = الأرواح

tutorials-module-tourney = البطولة

tutorials-module-collection = المجموعة

tutorials-module-badgewall = جدار الشارات

tutorials-module-streak = السلسلة

tutorials-module-voucher_builder = منشئ القسائم

tutorials-module-voucher = القسيمة

tutorials-module-social_graph = الرسم البياني الاجتماعي

tutorials-module-social_feed = الخلاصة الاجتماعية

tutorials-module-auto_share = المشاركة التلقائية

tutorials-module-share_to_win = شارك لتربح

tutorials-module-energy_invite = دعوة الطاقة

tutorials-module-friend_challenge = تحدي الأصدقاء

tutorials-module-ladder_climb = تسلق السلم

tutorials-module-streak_rescue = إنقاذ السلسلة

tutorials-module-leaderboard = لوحة المتصدرين

tutorials-module-network_effect = تأثير الشبكة

tutorials-module-score_to_coupon = النقاط → قسيمة

tutorials-module-energy = الطاقة

tutorials-module-upsell = البيع الإضافي

tutorials-module-redemption_store = متجر الاستبدال

tutorials-module-rate_limit = حد المعدل

tutorials-module-group_actions = إجراءات المجموعة

tutorials-module-groupbuy = الشراء الجماعي

tutorials-module-atomic_group = المجموعة الذرية

tutorials-module-pricecut = خفض السعر

tutorials-module-coop_quest = مهمة تعاونية

tutorials-module-raid = غارة

tutorials-module-squad = فرقة

tutorials-module-territory = منطقة

tutorials-module-gift_sending = إرسال هدية

tutorials-module-trading_post = مركز تجاري

tutorials-module-group_reward = مكافأة جماعية

tutorials-module-fcfs = الأول يأخذ الأول

tutorials-module-limited_drop = إسقاط محدود

tutorials-module-triggers = محفزات

tutorials-step-intro = سنرشدك خلال إعداد "{ $recipe_name }". { $module_count ->
        [one] وحدة واحدة
        *[other] { $module_count } وحدات
    } و { $rule_count ->
        [one] قاعدة واحدة
        *[other] { $rule_count } قواعد
    }.

tutorials-step-navigate-engagement = انقر على "التفاعل" في الشريط الجانبي لفتح سوق الوحدات

tutorials-step-navigate-vouchers = افتح "القسائم" في الشريط الجانبي لضبط قوالب القسائم

tutorials-step-navigate-rules = افتح "القواعد" في الشريط الجانبي لضبط قواعد الأحداث

tutorials-step-enable-module = تفعيل وحدة { $module_name }

tutorials-step-configure-module = ضبط { $module_name }: { $params_summary }

tutorials-step-create-voucher-template = إنشاء قالب قسيمة: { $template_summary }

tutorials-step-create-rule = إنشاء قاعدة: عند { $trigger_event } → { $actions_summary }

tutorials-step-test-action = لنجرب محاكاة "{ $event_name }" لاختبار القواعد

tutorials-step-celebrate = تم! إعداد "{ $recipe_name }" يعمل الآن.

conditions-blocker-supply_exhausted = تم استنفاد الكمية المتاحة لهذه الحملة بالكامل.

conditions-blocker-budget_exhausted = تم استنفاد ميزانية هذه الحملة بالكامل.

conditions-blocker-tier_required = مطلوب مستوى أعلى لهذه الحملة.

conditions-blocker-first_time_only = هذه الحملة مخصصة للمشاركين لأول مرة فقط.

conditions-blocker-user_segment_excluded = أنت لست ضمن شريحة المستخدمين المؤهلين.

conditions-blocker-user_segment_not_included = أنت لست ضمن شريحة المستخدمين المؤهلين.

conditions-blocker-min_account_age_days = حسابك جديد جدًا ولا يمكنه المشاركة بعد.

conditions-blocker-user_attribute_filter = حسابك لا يتطابق مع السمات المطلوبة.

conditions-blocker-frequency_per_user_per_day = لقد وصلت إلى الحد اليومي. حاول مرة أخرى غدًا.

conditions-blocker-frequency_per_user_per_week = لقد وصلت إلى الحد الأسبوعي.

conditions-blocker-frequency_per_user_per_month = لقد وصلت إلى الحد الشهري.

conditions-blocker-frequency_per_user_total = لقد وصلت إلى الحد الإجمالي لهذه الحملة.

conditions-blocker-frequency_global_per_day = تم الوصول إلى الحد العالمي اليوم.

conditions-blocker-time_not_yet_started = الحملة لم تبدأ بعد.

conditions-blocker-time_already_ended = الحملة قد انتهت.

conditions-blocker-time_invalid_day_of_week = الحملة غير متاحة اليوم.

conditions-blocker-time_invalid_hour = الحملة غير متاحة في هذا الوقت.

conditions-blocker-action_prerequisites_unmet = لم يتم استكمال الإجراءات المطلوبة مسبقًا.

conditions-blocker-campaign_not_found = لم يتم العثور على الحملة.

conditions-blocker-reservation_not_found = لم يتم العثور على الحجز أو انتهت صلاحيته.

conditions-blocker-reservation_already_committed = تم تأكيد الحجز بالفعل.

conditions-blocker-reservation_already_refunded = تم استرداد الحجز بالفعل.

conditions-blocker-reservation_expired = انتهت صلاحية الحجز؛ يرجى إعادة المحاولة.

conditions-blocker-commit_contention = تعارض عالٍ في الإيداع؛ يرجى إعادة المحاولة.

welcome_kit-item-table_stand-title = حامل الطاولة (A5، وجهين)

welcome_kit-item-table_stand-desc = حامل مكتب مقاس A5 مع رمز استجابة سريعة للدعوة إلى الإجراء على كلا الوجهين.

welcome_kit-item-counter_standing-title = حامل العدادات (A4)

welcome_kit-item-counter_standing-desc = عرض عمودي مقاس A4 للعداد أو منطقة الاستقبال.

welcome_kit-item-door_sticker-title = ملصق الباب (150 مم دائري)

welcome_kit-item-door_sticker-desc = ملصق نافذة / باب ثابت يدعو المارة للمسح.

welcome_kit-item-social_poster-title = ملصق اجتماعي (1080×1080)

welcome_kit-item-social_poster-desc = ملصق مربع جاهز لـ Instagram وFacebook وTikTok.

welcome_kit-item-handover_kit-title = حزمة التسليم الكاملة

welcome_kit-item-handover_kit-desc = جميع الأصول المذكورة أعلاه مجمعة في فهرس HTML واحد.

welcome_kit-default-tagline = امسح للعب. اربح مكافآت.

recipe_generator-match-found = تم العثور على الوصفة '{ $recipe_name }' من المكتبة.

recipe_generator-match-score = درجة المطابقة { $score }؛ الأسباب: { $reasons }.

recipe_generator-summary-untitled = بدون عنوان

recipe_generator-summary-empty-modules = لا شيء

recipe_generator-summary-recipe-includes = تتضمن الوصفة '{ $recipe_name }' { $module_count ->
        [one] وحدة واحدة
        *[other] { $module_count } وحدة
    }: { $module_list }، متصلة بـ { $rule_count ->
        [one] قاعدة واحدة
        *[other] { $rule_count } قواعد
    }.

recipe_generator-heuristic-fallback = (قالب استدلالي) مطابقة الوحدات ذات الصلة والقواعد الافتراضية من الكلمات المفتاحية.

recipe_generator-default-description = ادعُ 10 أصدقاء، واحصل على قسيمة قهوة مجانية.

modules-status-active = نشط

modules-status-inactive = غير نشط

modules-status-coming_soon = قريبًا

modules-action-enable = تفعيل

modules-action-disable = تعطيل

modules-action-configure = تكوين

error-internal = حدث خطأ داخلي. يرجى المحاولة مرة أخرى قريبًا.

error-not_found = لم يتم العثور على المورد المطلوب.

error-unauthorized = يلزم المصادقة.

error-forbidden = ليس لديك إذن لأداء هذا الإجراء.

error-validation = فشل التحقق من صحة بيانات الطلب.

error-rate_limited = لقد تجاوزت الحد المسموح به. حاول مرة أخرى لاحقًا.

error-conflict = يتعارض الطلب مع الحالة الحالية للمورد.

common-cta-login = تسجيل الدخول

common-cta-logout = تسجيل الخروج

common-cta-signup = اشتراك

common-cta-cancel = إلغاء

common-cta-save = حفظ

common-cta-confirm = تأكيد

common-cta-back = رجوع

common-cta-next = التالي

common-cta-loading = جاري التحميل…

common-nav-home = الرئيسية

common-nav-portal = البوابة

common-nav-storefront = واجهة المتجر

common-nav-play = تشغيل

common-nav-connect = الاتصال

common-currency-sgd = دولار سنغافوري

common-currency-cny = يوان صيني

common-currency-usd = دولار أمريكي
