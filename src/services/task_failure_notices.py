"""Safe, shared fault wording; no response bodies or credentials are accepted."""
from src.services.account_strategy_service import uses_ass_account


WEBSITE_FAULTS = {
    'site_login_required': (
        '闲鱼登录失效', '网站要求重新登录。',
        '先在真实浏览器确认闲鱼已登录，再用 ASS 插件执行完整会话同步。',
    ),
    'site_risk_control': (
        '闲鱼需要验证', '网站触发验证或风控，不等于 Cookie 过期。',
        '先停止重试，在真实浏览器完成验证并确认登录；会话更新后用 ASS 插件执行完整会话同步。',
    ),
    'browser_timeout': (
        '闲鱼操作超时', '页面或响应超时，不能确定登录失效。',
        '检查网络和真实浏览器页面状态；不要仅因超时更换 Cookie。',
    ),
    'browser_operation_failed': (
        '闲鱼任务失败', '浏览器操作失败，原因未确定。',
        '检查任务日志与真实浏览器页面状态。',
    ),
}

_SOURCE_AUTH = ('ASS 读取授权异常', 'ASS 拒绝提供会话，不代表闲鱼 Cookie 过期。',
                '检查 Goofish 消费者 Key 的有效期、绑定和读取权限。')
_SOURCE_NETWORK = ('ASS 连接异常', '本次无法从 ASS 读取会话，尚未验证闲鱼登录状态。',
                   '检查 Goofish 到 ASS 的连接与服务状态，不要反复同步 Cookie。')
_SOURCE_SESSION = ('ASS 会话不可用', 'ASS 中的会话缺失、不完整或格式不兼容，不能据此判断网站登录失效。',
                   '确认插件选中了正确账号；在闲鱼确认登录后执行完整会话同步，并核对消费端绑定。')
_SOURCE_CONFIG = ('ASS 配置异常', '消费端配置、Key 文件或身份绑定校验未通过。',
                  '检查 Goofish 的 ASS 配置、Key 文件权限与会话绑定；不要仅更换 Cookie。')
SOURCE_FAULTS = {
    **dict.fromkeys(('ass_authentication_rejected', 'ass_access_denied'), _SOURCE_AUTH),
    **dict.fromkeys(('ass_unavailable', 'ass_rate_limited'), _SOURCE_NETWORK),
    **dict.fromkeys(('ass_secret_unavailable', 'ass_session_invalid', 'ass_session_invalidated',
                    'ass_mixed_requires_opt_in', 'ass_cookie_missing',
                    'ass_cookie_samesite_unrepresentable', 'ass_response_invalid'), _SOURCE_SESSION),
    **dict.fromkeys(('ass_configuration_invalid', 'ass_key_file_invalid', 'ass_identity_mismatch',
                    'ass_binding_changed'), _SOURCE_CONFIG),
    **dict.fromkeys(('ass_browser_setup_failed', 'ass_browser_context_closed'),
                   ('浏览器会话恢复失败', 'ASS 会话未能在浏览器中恢复，尚未确认网站登录是否有效。',
                    '检查浏览器运行环境及会话格式；不要反复同步或自动解除暂停。')),
    'ass_source_failed': ('ASS 来源异常', '会话来源处理失败，原因尚未确认。',
                          '检查 ASS 来源状态和消费端配置，不要把它直接当作 Cookie 过期。'),
}


def safe_source_reason(reason: str) -> str:
    """Only fixed locally generated categories may leave the trusted consumer."""
    return reason if reason in SOURCE_FAULTS else 'ass_source_failed'


def build_task_failure_notice(task_config: dict, reason: str, *, phase: str,
                              paused_until, consecutive: int, threshold: int = 3):
    source_fault = reason in SOURCE_FAULTS
    title, summary, action = (SOURCE_FAULTS[reason] if source_fault else
                             WEBSITE_FAULTS.get(reason, WEBSITE_FAULTS['browser_operation_failed']))
    ass_mode = uses_ass_account(task_config)
    if not ass_mode:
        action = action.replace('用 ASS 插件执行完整会话同步', '更新所选登录状态文件')
    task_name = task_config.get('task_name', '未命名任务')
    body = f'{summary}\n任务: {task_name}\n阶段: {phase}\n'
    if source_fault:
        body += ('状态: 本次跳过；未增加网站失败计数，未新增网站暂停。\n'
                 '来源故障提醒每任务最多 1 小时一次。\n')
    else:
        pause = paused_until.strftime('%Y-%m-%d %H:%M:%S %z') if paused_until else '无'
        body += f'连续失败: {consecutive}/{threshold}\n暂停到: {pause}\n'
    body += action
    if ass_mode:
        body += '\n同步本身不会解除已有暂停；修复并验证后手动解除，或等待暂停到期。'
    elif not source_fault:
        body += '\n更新所选登录状态文件后可自动恢复。'
    return {'商品标题': f'{title}｜{task_name}', '当前售价': 'N/A', '商品链接': '#',
            'notification_type': 'task_failure'}, body
