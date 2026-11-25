import Foundation
import StoreKit
import StoreKitTest
import XCTest

final class PleazyngTrialReviewUITests: XCTestCase {
    private let appBundleID = "org.snapetech.pleazyng"
    private let trialProductID = "pleazyng_7_day_trial"
    private let lifetimeProductID = "pleazyng_lifetime_unlock"

    func testCaptureRealTrialOfferAndActiveAccess() throws {
        let session = try makeLocalTestSession()
        let app = XCUIApplication(bundleIdentifier: appBundleID)
        app.launch()

        var appBehavior: [String: Any] = [
            "lifetimePurchaseActivated": false,
            "lifetimeTransactionCreated": false
        ]
        let purchase = try purchaseTrial(in: app, session: session, appBehavior: &appBehavior)
        captureFilesAndBooks(in: app, appBehavior: &appBehavior)
        let restoredHistory = try restoreTrial(
            in: app, session: session, transaction: purchase.transaction, appBehavior: &appBehavior
        )
        let relaunchedHistory = try verifyFreshRelaunch(
            in: app, session: session, transaction: purchase.transaction, appBehavior: &appBehavior
        )
        captureOpenMediaForm(in: app, appBehavior: &appBehavior)
        try attachLocalTestRecord(
            transactions: session.allTransactions(),
            transaction: purchase.transaction,
            history: ["afterRestore": restoredHistory, "afterFreshRelaunch": relaunchedHistory],
            productQueries: purchase.productQueries,
            appBehavior: appBehavior
        )
    }
}

private extension PleazyngTrialReviewUITests {
    func makeLocalTestSession() throws -> SKTestSession {
        let configurationURL = try XCTUnwrap(
            Bundle(for: PleazyngTrialReviewUITests.self).url(
                forResource: "pleazyng-trial-review",
                withExtension: "storekit"
            ),
            "The local StoreKit Test configuration was not bundled with the test runner"
        )
        let session = try SKTestSession(contentsOf: configurationURL)
        session.resetToDefaultState()
        session.storefront = "USA"
        session.locale = Locale(identifier: "en_US")
        session.disableDialogs = true
        session.clearTransactions()
        XCTAssertTrue(
            session.allTransactions().isEmpty,
            "The LOCALTEST store session must start without prior transactions"
        )
        return session
    }

    func purchaseTrial(
        in app: XCUIApplication,
        session: SKTestSession,
        appBehavior: inout [String: Any]
    ) throws -> (transaction: SKTestTransaction, productQueries: [String: Any]) {
        let trialButton = app.buttons["Start my 7-day free trial"]
        XCTAssertTrue(trialButton.waitForExistence(timeout: 90), "The app did not load its real StoreKit trial product")
        // The 13-inch iPad Simulator can report this visible Flutter button as
        // not hittable even though XCTest's real tap completes the purchase.
        // The verified LOCALTEST transaction and opened Files and Books screen
        // below prove the action succeeded.
        let lifetimePrice = lifetimeOffer(in: app)
        XCTAssertTrue(
            lifetimePrice.waitForExistence(timeout: 60),
            "The app did not display the queried LOCALTEST lifetime price"
        )
        XCTAssertTrue(
            app.staticTexts.containing(
                NSPredicate(format: "label CONTAINS[c] %@", "There is no subscription or automatic charge")
            ).firstMatch.exists,
            "The live trial disclosure was not visible"
        )
        let productQueries: [String: Any] = [
            "trialProductVisibleInApp": trialButton.exists,
            "lifetimeProductVisibleInApp": lifetimePrice.exists,
            "lifetimeDisplayPrice": lifetimePrice.label,
            "storefront": "USA"
        ]
        attachScreenshot(of: app, named: "pleazyng-trial-offer-localtest")

        // This is the actual app's UI action. Do not call SKTestSession.buyProduct
        // or activate the lifetime purchase button anywhere in this test.
        trialButton.tap()
        let useFiles = app.buttons["Use files and books"]
        XCTAssertTrue(
            useFiles.waitForExistence(timeout: 90),
            "The app did not leave the trial gate after its StoreKit transaction was verified"
        )
        appBehavior["trialGateOpenedAfterLivePurchaseAction"] = useFiles.exists

        let trialTransactions = session.allTransactions().filter { $0.productIdentifier == trialProductID }
        XCTAssertEqual(trialTransactions.count, 1, "Expected exactly one LOCALTEST trial transaction")
        let transaction = try XCTUnwrap(trialTransactions.first)
        XCTAssertEqual(transaction.state, .purchased, "The LOCALTEST trial transaction did not complete")
        XCTAssertFalse(
            session.allTransactions().contains { $0.productIdentifier == lifetimeProductID },
            "The test must never create a lifetime purchase transaction"
        )
        return (transaction, productQueries)
    }

    func lifetimeOffer(in app: XCUIApplication) -> XCUIElement {
        app.buttons.matching(
            NSPredicate(format: "label CONTAINS %@ AND label CONTAINS %@", "Unlock every feature", "$2.99")
        ).firstMatch
    }

    func captureFilesAndBooks(in app: XCUIApplication, appBehavior: inout [String: Any]) {
        let useFiles = app.buttons["Use files and books"]
        useFiles.tap()
        let addSource = app.buttons["Add source"]
        XCTAssertTrue(addSource.waitForExistence(timeout: 60), "The actual local Files and Books screen did not open")
        XCTAssertTrue(app.buttons["Import files"].exists, "The actual local screen is missing its import action")
        XCTAssertTrue(
            app.staticTexts.containing(
                NSPredicate(format: "label CONTAINS[c] %@", "Read, listen, and watch from your own files")
            )
                .firstMatch.exists,
            "Expected the real empty local library state with no server or seeded media"
        )
        appBehavior["localFilesAndBooksScreenOpened"] = addSource.exists
        attachScreenshot(of: app, named: "pleazyng-trial-active-files-books-localtest")
    }

    func showLifetimeSheet(in app: XCUIApplication) {
        let homeTab = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Home")).firstMatch
        XCTAssertTrue(homeTab.waitForExistence(timeout: 30), "The real Home navigation action was not available")
        homeTab.tap()
        // The icon font contributes a leading `F\n` to this button's accessibility label.
        let profilesMenu = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Profiles")).firstMatch
        XCTAssertTrue(profilesMenu.waitForExistence(timeout: 30), "The real Profiles menu was not available on Home")
        attachAccessibilityHierarchy(of: app, named: "trial-settings-home-accessibility-localtest")
        profilesMenu.tap()
        let settingsItem = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Settings")).firstMatch
        XCTAssertTrue(
            settingsItem.waitForExistence(timeout: 30),
            "The real Settings action was not available in the Profiles menu"
        )
        attachAccessibilityHierarchy(of: app, named: "trial-settings-profiles-menu-accessibility-localtest")
        settingsItem.tap()
        let lifetimeTile = app.descendants(matching: .any).matching(
            NSPredicate(format: "label CONTAINS %@", "Pleazyng lifetime access")
        ).firstMatch
        XCTAssertTrue(lifetimeTile.waitForExistence(timeout: 60), "The actual lifetime-access Settings row was not available")
        tapWhenHittable(lifetimeTile, in: app, failureMessage: "The lifetime-access Settings row was not reachable")
    }

    func restoreTrial(
        in app: XCUIApplication,
        session: SKTestSession,
        transaction: SKTestTransaction,
        appBehavior: inout [String: Any]
    ) throws -> [String: Any] {
        showLifetimeSheet(in: app)
        let restoreButton = app.buttons.matching(
            NSPredicate(format: "label CONTAINS %@", "Restore lifetime purchase")
        ).firstMatch
        XCTAssertTrue(restoreButton.waitForExistence(timeout: 60), "The real lifetime sheet did not show Restore")
        XCTAssertTrue(lifetimeOffer(in: app).waitForExistence(timeout: 60), "The lifetime price disappeared")
        let activeTrial = app.staticTexts["Your 7-day full-feature trial is active."]
        XCTAssertTrue(activeTrial.waitForExistence(timeout: 30), "StoreKit history did not show the active trial")

        let restoreActionWindowStartedAt = Date()
        restoreButton.tap()
        let restoreActionTapReturnedAt = Date()
        // StoreKit sync can finish before XCTest's next accessibility poll, so
        // validate the stable app/history state instead of requiring a sampled spinner.
        XCTAssertTrue(
            activeTrial.waitForExistence(timeout: 90),
            "The active trial did not remain visible after tapping Restore"
        )
        XCTAssertFalse(
            app.staticTexts["No Pleazyng lifetime purchase was found for this store account."].exists,
            "A verified trial history should let the actual restore path complete without claiming lifetime ownership"
        )
        XCTAssertFalse(
            session.allTransactions().contains { $0.productIdentifier == lifetimeProductID },
            "Restore must not create or buy a lifetime transaction"
        )
        let history = try verifyHistory(session.allTransactions(), matching: transaction, after: "Restore")
        appBehavior["activeTrialShownByAppAfterRestore"] = activeTrial.exists
        let restoreActionWindowCompletedAt = Date()
        appBehavior["restoreActionTapCompleted"] = true
        appBehavior["restoreActionTiming"] = [
            "windowStartedAtEpochMilliseconds": Int64(restoreActionWindowStartedAt.timeIntervalSince1970 * 1000),
            "tapReturnedAtEpochMilliseconds": Int64(restoreActionTapReturnedAt.timeIntervalSince1970 * 1000),
            "windowCompletedAtEpochMilliseconds": Int64(restoreActionWindowCompletedAt.timeIntervalSince1970 * 1000),
            "preRelaunchAtEpochMilliseconds": Int64(restoreActionWindowCompletedAt.timeIntervalSince1970 * 1000),
            "timezoneOffsetSeconds": TimeZone.current.secondsFromGMT(for: restoreActionWindowStartedAt),
            "windowEndsBeforeFreshRelaunch": true
        ] as [String: Any]
        attachScreenshot(of: app, named: "pleazyng-trial-active-supporter-localtest")
        return history
    }

    func verifyFreshRelaunch(
        in app: XCUIApplication,
        session: SKTestSession,
        transaction: SKTestTransaction,
        appBehavior: inout [String: Any]
    ) throws -> [String: Any] {
        app.terminate()
        app.launch()
        let relaunchedLocalScreen = app.buttons["Add source"]
        XCTAssertTrue(
            relaunchedLocalScreen.waitForExistence(timeout: 90),
            "Fresh launch did not restore the active trial from the app's verified StoreKit history"
        )
        let history = try verifyHistory(session.allTransactions(), matching: transaction, after: "Fresh launch")
        appBehavior["activeTrialRestoredAfterFreshRelaunch"] = relaunchedLocalScreen.exists
        return history
    }

    func verifyHistory(
        _ history: [SKTestTransaction],
        matching transaction: SKTestTransaction,
        after stage: String
    ) throws -> [String: Any] {
        XCTAssertEqual(history.count, 1, "\(stage) must preserve exactly one LOCALTEST trial transaction")
        let trial = try XCTUnwrap(history.first)
        XCTAssertEqual(trial.productIdentifier, trialProductID)
        XCTAssertEqual(
            trial.originalTransactionIdentifier,
            transaction.originalTransactionIdentifier,
            "\(stage) must preserve the original LOCALTEST trial transaction identity"
        )
        XCTAssertEqual(trial.purchaseDate, transaction.purchaseDate, "\(stage) changed the original purchase date")
        return [
            "transactionCount": history.count,
            "originalTestTransactionIdentifier": trial.originalTransactionIdentifier,
            "originalPurchaseDate": ISO8601DateFormatter().string(from: trial.purchaseDate)
        ]
    }
}

private extension PleazyngTrialReviewUITests {
    func captureOpenMediaForm(in app: XCUIApplication, appBehavior: inout [String: Any]) {
        let openMediaAction = app.buttons["Open media"]
        XCTAssertTrue(
            openMediaAction.waitForExistence(timeout: 60),
            "The local source screen did not expose the actual Open media action"
        )
        tapWhenHittable(openMediaAction, in: app, failureMessage: "The actual Open media action was not reachable")
        let mediaAddressField = app.textFields["Media address"]
        XCTAssertTrue(mediaAddressField.waitForExistence(timeout: 30), "The real Open media form did not open")
        XCTAssertTrue(app.staticTexts["Open media"].exists, "The actual Open media form title was not visible")
        XCTAssertTrue(
            app.textFields["Display name (optional)"].exists,
            "The real optional display-name field was missing"
        )
        let keychainWarning = app.staticTexts.containing(
            NSPredicate(format: "label CONTAINS[c] %@", "Couldn't unlock the saved streaming settings")
        ).firstMatch
        XCTAssertFalse(keychainWarning.exists, "The Open media form must not show a saved-streaming Keychain error")
        XCTAssertFalse(app.alerts.firstMatch.exists, "The Open media form must not contain an XCTest or system alert")
        appBehavior["openMediaFormOpenedAfterTrial"] = mediaAddressField.exists
        appBehavior["openMediaAddressFieldVisible"] = mediaAddressField.exists
        appBehavior["openMediaFormKeychainWarningVisible"] = keychainWarning.exists
        appBehavior["openMediaFormSystemAlertVisible"] = app.alerts.firstMatch.exists
        attachScreenshot(of: app, named: "pleazyng-open-media-form-localtest")
    }

    func attachLocalTestRecord(
        transactions: [SKTestTransaction],
        transaction: SKTestTransaction,
        history: [String: [String: Any]],
        productQueries: [String: Any],
        appBehavior: [String: Any]
    ) throws {
        let record: [String: Any] = [
            "environment": "LOCALTEST",
            "qualification": "Xcode StoreKitTest local configuration only; not App Store Connect or App Store Sandbox evidence.",
            "productQueries": productQueries,
            "trialTransaction": [
                "productID": transaction.productIdentifier,
                "state": "purchased",
                "testTransactionIdentifier": transaction.identifier,
                "originalTestTransactionIdentifier": transaction.originalTransactionIdentifier,
                "purchaseDate": ISO8601DateFormatter().string(from: transaction.purchaseDate),
                "expirationDate": optionalDate(transaction.expirationDate),
                "cancelDate": optionalDate(transaction.cancelDate)
            ],
            "trialHistorySnapshots": history,
            "appBehavior": appBehavior,
            "allLocalTestTransactions": transactions.map { item in
                [
                    "productID": item.productIdentifier,
                    "state": item.state == .purchased ? "purchased" : "other",
                    "testTransactionIdentifier": item.identifier,
                    "originalTestTransactionIdentifier": item.originalTransactionIdentifier,
                    "purchaseDate": ISO8601DateFormatter().string(from: item.purchaseDate)
                ]
            }
        ]
        let data = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
        let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
        attachment.name = "pleazyng-trial-storekit-localtest-record"
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    func optionalDate(_ value: Date?) -> Any {
        guard let value else { return NSNull() }
        return ISO8601DateFormatter().string(from: value)
    }

    func tapWhenHittable(
        _ element: XCUIElement,
        in app: XCUIApplication,
        failureMessage: String
    ) {
        for _ in 0..<6 {
            if element.isHittable {
                element.tap()
                return
            }
            app.swipeUp()
        }
        guard element.isHittable else {
            XCTFail(failureMessage)
            return
        }
        element.tap()
    }

    func attachAccessibilityHierarchy(of app: XCUIApplication, named name: String) {
        let attachment = XCTAttachment(data: Data(app.debugDescription.utf8), uniformTypeIdentifier: "public.plain-text")
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    func attachScreenshot(of app: XCUIApplication, named name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
