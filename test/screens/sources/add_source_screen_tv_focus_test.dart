import 'dart:io';

import 'package:drift/native.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:plezy/focus/focusable_text_field.dart';
import 'package:plezy/i18n/strings.g.dart';
import 'package:plezy/providers/trackers_provider.dart';
import 'package:plezy/screens/sources/add_source_screen.dart';
import 'package:plezy/sources/source_controller.dart';
import 'package:plezy/sources/source_database.dart';
import 'package:plezy/sources/source_models.dart';
import 'package:plezy/theme/mono_theme.dart';
import 'package:plezy/utils/platform_detector.dart';
import 'package:provider/provider.dart';

import '../../sources/fixtures/memory_sources.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late Directory directory;
  late SourceDatabase database;
  late MemorySources sources;
  late TrackersProvider trackers;

  setUp(() async {
    debugDefaultTargetPlatformOverride = TargetPlatform.android;
    directory = await Directory.systemTemp.createTemp('pleazyng-tv-source-focus-');
    database = SourceDatabase(NativeDatabase.memory());
    sources = MemorySources(SourceRepository(database), directory);
    trackers = TrackersProvider();
    LocaleSettings.setLocaleSync(AppLocale.en);
    await TvDetectionService.getInstance(forceTv: true);
  });

  tearDown(() async {
    sources.dispose();
    trackers.dispose();
    await database.close();
    await directory.delete(recursive: true);
    TvDetectionService.debugReset();
    debugDefaultTargetPlatformOverride = null;
  });

  testWidgets('Android TV Book Orbit credentials traverse with the D-pad and native IME Next', (tester) async {
    try {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(1200, 1600);
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<SourceController>.value(value: sources),
            ChangeNotifierProvider<TrackersProvider>.value(value: trackers),
          ],
          child: MaterialApp(
            theme: monoTheme(MonoPalette.dark).copyWith(platform: TargetPlatform.android),
            home: const AddSourceScreen(initialKind: FileSourceKind.bookorbit),
          ),
        ),
      );
      await tester.pumpAndSettle();

      final username = find.byWidgetPredicate(
        (widget) => widget is FocusableTextFormField && widget.decoration?.labelText == t.fileSources.username,
      );
      final password = find.byWidgetPredicate(
        (widget) => widget is FocusableTextFormField && widget.decoration?.labelText == t.fileSources.password,
      );
      final usernameConfig = tester.widget<FocusableTextFormField>(username);
      final passwordConfig = tester.widget<FocusableTextFormField>(password);
      for (final field in [usernameConfig, passwordConfig]) {
        expect(field.textInputAction, TextInputAction.next);
        expect(field.tvTextInputAutoOpenBehavior, TvTextInputAutoOpenBehavior.never);
      }

      final usernameField = find.descendant(of: username, matching: find.byType(EditableText));
      final passwordField = find.descendant(of: password, matching: find.byType(EditableText));
      final usernameNode = tester.widget<EditableText>(usernameField).focusNode;
      final passwordNode = tester.widget<EditableText>(passwordField).focusNode;

      await tester.ensureVisible(username);
      await tester.pumpAndSettle();
      usernameNode.requestFocus();
      await tester.pumpAndSettle();
      expect(usernameNode.hasFocus, isTrue);
      expect(tester.widget<EditableText>(usernameField).readOnly, isTrue, reason: 'focus alone must not open the IME');

      // Without a keyboard session, D-pad Down must reach Password.
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pumpAndSettle();
      expect(passwordNode.hasFocus, isTrue);
      expect(tester.widget<EditableText>(passwordField).readOnly, isTrue);

      // Return to Username, explicitly activate the native keyboard with OK,
      // then emulate the Android IME's Next action (the reported failure path).
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
      await tester.pumpAndSettle();
      expect(usernameNode.hasFocus, isTrue);
      await tester.sendKeyEvent(LogicalKeyboardKey.select);
      await tester.pumpAndSettle();
      expect(tester.widget<EditableText>(usernameField).readOnly, isFalse);
      tester.testTextInput.enterText('test-viewer');
      await tester.pump();
      await tester.testTextInput.receiveAction(TextInputAction.next);
      await tester.pumpAndSettle();
      expect(passwordNode.hasFocus, isTrue, reason: 'native IME Next must move from Username to Password');
      expect(
        tester.widget<EditableText>(passwordField).readOnly,
        isTrue,
        reason: 'arriving at Password must not immediately reopen its keyboard',
      );

      // Password remains editable on explicit OK, even with auto-open disabled.
      await tester.sendKeyEvent(LogicalKeyboardKey.select);
      await tester.pumpAndSettle();
      expect(tester.widget<EditableText>(passwordField).readOnly, isFalse);
    } finally {
      // Flutter verifies foundation debug overrides before outer tearDown.
      // Clear this here, including when an assertion throws, so the test
      // does not fail post-test invariant checks on Windows.
      debugDefaultTargetPlatformOverride = null;
    }
  });
}
