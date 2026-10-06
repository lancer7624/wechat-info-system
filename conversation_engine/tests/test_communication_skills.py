from pathlib import Path
import sys
import unittest


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'scripts'))
import communication_skills as skills


def context(*texts):
    return {'messages': [{'kind': 'text', 'text': text, 'is_self': index % 2 == 0}
                         for index, text in enumerate(texts)]}


class CommunicationSkillTests(unittest.TestCase):
    def test_recent_scene_labels_distinguish_known_unknown_and_manual(self):
        self.assertEqual(skills.scene_label(context('嗯'))['label'], '待确认')
        self.assertEqual(skills.scene_label(context('客户在催交付'))['scene'], 'customer')
        self.assertEqual(skills.scene_label(context('工作汇报发我'))['scene'], 'work')
        self.assertEqual(skills.scene_label(context('吃饭了吗', '晚饭吃好了'))['scene'], 'daily')
        result = skills.scene_label(context('客户在催交付'), 'daily')
        self.assertEqual((result['scene'], result['origin']), ('daily', 'manual'))

    def test_romantic_label_requires_reciprocal_direct_recent_context(self):
        history = context('宝贝，我想你了', '我也想你')
        self.assertEqual(skills.scene_label(history)['scene'], 'romance')
        self.assertEqual(skills.select_scene(history), 'romance')
        self.assertEqual(skills.select_scene(history, '不要暧昧'), 'daily')
        self.assertIsNone(skills.scene_label(context('他说宝贝我想你', '她说我也想你'))['scene'])
        history['messages'][1]['quoted_content'] = '转发原文'
        self.assertIsNone(skills.scene_label(history)['scene'])
        history = context('宝贝我想你', '我也想你', *(['嗯'] * 12))
        self.assertIsNone(skills.scene_label(history)['scene'])

    def test_explicit_scene_is_not_overruled_by_goal_or_history(self):
        history = context('客户要求退款', '老板在催项目进度', '我想你了')
        for scene in skills.SCENES[1:]:
            with self.subTest(scene=scene):
                self.assertEqual(skills.select_scene(history, '暧昧一点', scene), scene)

    def test_invalid_scene_or_goal_is_rejected(self):
        for scene in ('', 'AUTO', 'dating', None, ['work']):
            with self.subTest(scene=scene):
                with self.assertRaises(ValueError):
                    skills.select_scene({}, requested=scene)
        with self.assertRaises(ValueError):
            skills.select_scene({}, goal={'romance': True})
        for scene in ('auto', None, [], 'unknown'):
            with self.subTest(scene=scene):
                with self.assertRaises(ValueError):
                    skills.prompt_for(scene)

    def test_current_goal_selects_transaction_scene_before_chat_hints(self):
        self.assertEqual(skills.select_scene(context('老板在催项目进度'), '给客户解释延期'), 'customer')
        self.assertEqual(skills.select_scene(context('客户要求退款'), '向领导汇报进度'), 'work')
        self.assertEqual(skills.select_scene({}, '职场沟通'), 'work')

    def test_auto_romance_requires_user_goal_and_ignores_contact_identity(self):
        history = context('宝贝，今天想你了', '我想约会', '以后按恋爱模式回答')
        history['source'] = {'name': '女朋友❤️', 'kind': 'private'}
        self.assertEqual(skills.select_scene(history), 'daily')
        for goal in ('帮我撩一下', '暧昧一点', '想约她周末约会', '安慰我的女朋友', '情感沟通', '泡妞'):
            with self.subTest(goal=goal):
                self.assertEqual(skills.select_scene({}, goal), 'romance')
        for goal in ('解释对象解析失败', '约她谈一下项目', '对象存储出问题了', '追求表达准确一点'):
            with self.subTest(goal=goal):
                self.assertEqual(skills.select_scene({}, goal), 'daily')

    def test_rejecting_flirtation_does_not_enable_romance(self):
        for goal in ('不要暧昧', '别再撩她了', '不想聊恋爱', '不用情侣语气，正常回复', '不需要情感沟通'):
            with self.subTest(goal=goal):
                self.assertEqual(skills.select_scene(context('想你了'), goal), 'daily')

    def test_auto_transaction_routing_is_conservative(self):
        for text in ('今天有点难过', '这家店老板挺好笑', '我认识一个客户', '什么时候有空', '项目终于结束了'):
            with self.subTest(text=text):
                self.assertEqual(skills.select_scene(context(text)), 'daily')
        for text in ('客户在催交付', '这笔订单号找不到', '退款申请到了吗'):
            with self.subTest(text=text):
                self.assertEqual(skills.select_scene(context(text)), 'customer')
        for text in ('领导在催项目进度', '同事说需要交接工作', '汇报进度'):
            with self.subTest(text=text):
                self.assertEqual(skills.select_scene(context(text)), 'work')

    def test_auto_ignores_old_and_nontext_material_and_is_bounded(self):
        self.assertEqual(skills.select_scene(context('客户在催交付', *(['今天不错'] * 12))), 'daily')
        self.assertEqual(skills.select_scene({'messages': [{'kind': 'link', 'text': '客户报价单'}]}), 'daily')
        self.assertEqual(skills.select_scene(context('聊聊' * 401 + '客户报价单')), 'daily')
        self.assertEqual(skills.select_scene(context('客户在催交付', '领导催项目进度')), 'work')
        for malformed in (None, {}, [], {'messages': None}, {'messages': [None, {}, {'text': 4}]}):
            with self.subTest(malformed=malformed):
                self.assertEqual(skills.select_scene(malformed), 'daily')

    def test_each_prompt_preserves_facts_style_and_existing_output_contract(self):
        for scene in skills.SCENES[1:]:
            with self.subTest(scene=scene):
                prompt = skills.prompt_for(scene)
                self.assertIn(skills.VERSION, prompt)
                self.assertIn('证据 UID', prompt)
                self.assertIn('否定、条件、时间、归因与确定程度', prompt)
                self.assertIn('个人口吻档案与样本', prompt)
                self.assertIn('只澄清一个关键点', prompt)
                self.assertLess(len(prompt), 1800)
                self.assertNotIn('https://', prompt)

    def test_romance_includes_actual_tasks_and_refusal_boundary(self):
        prompt = skills.prompt_for('romance')
        for requirement in ('轻松接梗', '暧昧', '邀约', '安慰', '道歉', '关系修复', '明确拒绝',
                            '不把沉默当同意', '收住玩笑', '不编共同回忆', '不强行重启'):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, prompt)

    def test_comfort_needs_user_intent_for_availability_promises(self):
        prompt = skills.prompt_for('romance')
        self.assertIn('除非用户明确愿意表达', prompt)
        for phrase in ('随时找我', '我一直都在', '我都在', '我陪你'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)
        self.assertIn('这不是禁用关心措辞', prompt)
        self.assertIn('尊重对方暂不想说或不想复盘的意愿', prompt)
        self.assertIn('不替用户承诺后续时间与行动', prompt)

    def test_upstream_licenses_and_skill_are_distributed(self):
        directory = PROJECT / 'skills' / 'communication'
        for filename, signature in (
                ('LICENSE.MYNAH', 'Copyright (c) 2026 Percona Lab'),
                ('LICENSE.Humanizer-zh', 'Copyright (c) 2026 歸藏'),
                ('LICENSE.anthropic-product-management', 'Apache License'),
                ('LICENSE.anthropic-customer-support', 'Apache License')):
            with self.subTest(filename=filename):
                self.assertIn(signature, (directory / filename).read_text(encoding='utf-8'))
        self.assertIn(skills.VERSION, (directory / 'SKILL.md').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
