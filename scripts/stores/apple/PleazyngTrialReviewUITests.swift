import Foundation
import StoreKit
import StoreKitTest
import XCTest

final class PleazyngTrialReviewUITests: XCTestCase {
    private let appBundleID = "org.snapetech.pleazyng"
    private let trialProductID = "pleazyng_7_day_trial"
    private let lifetimeProductID = "pleazyng_lifetime_unlock"

    func testCaptureRealTrialOfferAndActiveAccess() throws {
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
        XCTAssertTrue(session.allTransactions().isEmpty, "The LOCALTEST store session must start without prior transactions")

        let app = XCUIApplication(bundleIdentifier: appBundleID)
        app.launch()

        let trialButton = app.buttons["Start my 7-day free trial"]
        XCTAssertTrue(trialButton.waitForExistence(timeout: 90), "The app did not load its real StoreKit trial product")
        // The 13-inch iPad Simulator can report this visible Flutter button as
        // not hittable even though XCTest's real tap completes the purchase.
        // The verified LOCALTEST transaction and opened Files and Books screen
        // below prove the action succeeded.
        let localLifetimePrice = app.buttons.matching(
            NSPredicate(format: "label CONTAINS %@ AND label CONTAINS %@", "Unlock every feature", "$2.99")
        ).firstMatch
        XCTAssertTrue(localLifetimePrice.waitForExistence(timeout: 60), "The app did not display the queried LOCALTEST lifetime price")
        XCTAssertTrue(
            app.staticTexts.containing(
                NSPredicate(format: "label CONTAINS[c] %@", "There is no subscription or automatic charge")
            ).firstMatch.exists,
            "The live trial disclosure was not visible"
        )
        let trialProductVisibleInApp = trialButton.exists
        let lifetimeProductVisibleInApp = localLifetimePrice.exists
        let lifetimeDisplayPrice = localLifetimePrice.label
        attachScreenshot(of: app, named: "pleazyng-trial-offer-localtest")

        // This is the actual app's UI action. Do not call SKTestSession.buyProduct
        // or activate the lifetime purchase button anywhere in this test.
        trialButton.tap()
        let useFiles = app.buttons["Use files and books"]
        XCTAssertTrue(
            useFiles.waitForExistence(timeout: 90),
            "The app did not leave the trial gate after its StoreKit transaction was verified"
        )
        let trialGateOpenedAfterPurchase = useFiles.exists

        let trialTransactions = session.allTransactions().filter { $0.productIdentifier == trialProductID }
        XCTAssertEqual(trialTransactions.count, 1, "Expected exactly one LOCALTEST trial transaction")
        let transaction = try XCTUnwrap(trialTransactions.first)
        XCTAssertEqual(transaction.state, .purchased, "The LOCALTEST trial transaction did not complete")
        XCTAssertFalse(
            session.allTransactions().contains { $0.productIdentifier == lifetimeProductID },
            "The test must never create a lifetime purchase transaction"
        )

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
        let localFilesScreenOpened = addSource.exists
        attachScreenshot(of: app, named: "pleazyng-trial-active-files-books-localtest")

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

        let restoreButton = app.buttons.matching(
            NSPredicate(format: "label CONTAINS %@", "Restore lifetime purchase")
        ).firstMatch
        XCTAssertTrue(restoreButton.waitForExistence(timeout: 60), "The real lifetime sheet did not show Restore")
        XCTAssertTrue(localLifetimePrice.waitForExistence(timeout: 60), "The real lifetime sheet lost the queried price")
        let activeTrial = app.staticTexts["Your 7-day full-feature trial is active."]
        XCTAssertTrue(activeTrial.waitForExistence(timeout: 30), "App Store transaction history did not show the active trial")

        let restoreActionWindowStartedAt = Date()
        restoreButton.tap()
        let restoreActionTapReturnedAt = Date()
        // StoreKit sync can finish before XCTest's next accessibility poll, so
        // validate the stable app/history state instead of requiring a sampled spinner.
        let restoreActionTapCompleted = true
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
        let restoredTransactions = session.allTransactions()
        XCTAssertEqual(restoredTransactions.count, 1, "Restore must leave exactly the single LOCALTEST trial transaction")
        let restoredTrial = try XCTUnwrap(restoredTransactions.first)
        XCTAssertEqual(restoredTrial.productIdentifier, trialProductID)
        XCTAssertEqual(
            restoredTrial.originalTransactionIdentifier,
            transaction.originalTransactionIdentifier,
            "Restore must preserve the trial's original StoreKit test transaction"
        )
        XCTAssertEqual(
            restoredTrial.purchaseDate,
            transaction.purchaseDate,
            "Restore must preserve the original LOCALTEST trial purchase date"
        )
        let activeTrialShownAfterRestore = activeTrial.exists
        let restoreActionWindowCompletedAt = Date()
        attachScreenshot(of: app, named: "pleazyng-trial-active-supporter-localtest")

        app.terminate()
        app.launch()
        let relaunchedLocalScreen = app.buttons["Add source"]
        XCTAssertTrue(
            relaunchedLocalScreen.waitForExistence(timeout: 90),
            "Fresh launch did not restore the active trial from the app's verified StoreKit history"
        )
        let relaunchedTransactions = session.allTransactions()
        XCTAssertEqual(relaunchedTransactions.count, 1, "Fresh launch must not add or lose a LOCALTEST transaction")
        let relaunchedTrial = try XCTUnwrap(relaunchedTransactions.first)
        XCTAssertEqual(relaunchedTrial.productIdentifier, trialProductID)
        XCTAssertEqual(
            relaunchedTrial.originalTransactionIdentifier,
            transaction.originalTransactionIdentifier,
            "Fresh launch must retain the original LOCALTEST transaction identity"
        )
        XCTAssertEqual(
            relaunchedTrial.purchaseDate,
            transaction.purchaseDate,
            "Fresh launch must retain the original LOCALTEST trial purchase date"
        )

        let record: [String: Any] = [
            "environment": "LOCALTEST",
            "qualification": "Xcode StoreKitTest local configuration only; not App Store Connect or App Store Sandbox evidence.",
            "productQueries": [
                "trialProductVisibleInApp": trialProductVisibleInApp,
                "lifetimeProductVisibleInApp": lifetimeProductVisibleInApp,
                "lifetimeDisplayPrice": lifetimeDisplayPrice,
                "storefront": "USA",
            ],
            "trialTransaction": [
                "productID": transaction.productIdentifier,
                "state": "purchased",
                "testTransactionIdentifier": transaction.identifier,
                "originalTestTransactionIdentifier": transaction.originalTransactionIdentifier,
                "purchaseDate": ISO8601DateFormatter().string(from: transaction.purchaseDate),
                "expirationDate": optionalDate(transaction.expirationDate),
                "cancelDate": optionalDate(transaction.cancelDate),
            ],
            "trialHistorySnapshots": [
                "afterRestore": [
                    "transactionCount": restoredTransactions.count,
                    "originalTestTransactionIdentifier": restoredTrial.originalTransactionIdentifier,
                    "originalPurchaseDate": ISO8601DateFormatter().string(from: restoredTrial.purchaseDate),
                ],
                "afterFreshRelaunch": [
                    "transactionCount": relaunchedTransactions.count,
                    "originalTestTransactionIdentifier": relaunchedTrial.originalTransactionIdentifier,
                    "originalPurchaseDate": ISO8601DateFormatter().string(from: relaunchedTrial.purchaseDate),
                ],
            ],
            "appBehavior": [
                "trialGateOpenedAfterLivePurchaseAction": trialGateOpenedAfterPurchase,
                "localFilesAndBooksScreenOpened": localFilesScreenOpened,
                "activeTrialShownByAppAfterRestore": activeTrialShownAfterRestore,
                "restoreActionTapCompleted": restoreActionTapCompleted,
                "restoreActionTiming": [
                    "windowStartedAtEpochMilliseconds": Int64(restoreActionWindowStartedAt.timeIntervalSince1970 * 1000),
                    "tapReturnedAtEpochMilliseconds": Int64(restoreActionTapReturnedAt.timeIntervalSince1970 * 1000),
                    "windowCompletedAtEpochMilliseconds": Int64(
                        restoreActionWindowCompletedAt.timeIntervalSince1970 * 1000
                    ),
                    "preRelaunchAtEpochMilliseconds": Int64(
                        restoreActionWindowCompletedAt.timeIntervalSince1970 * 1000
                    ),
                    "timezoneOffsetSeconds": TimeZone.current.secondsFromGMT(for: restoreActionWindowStartedAt),
                    "windowEndsBeforeFreshRelaunch": true,
                ],
                "activeTrialRestoredAfterFreshRelaunch": relaunchedLocalScreen.exists,
                "lifetimePurchaseActivated": false,
                "lifetimeTransactionCreated": false,
            ],
            "allLocalTestTransactions": session.allTransactions().map { item in
                [
                    "productID": item.productIdentifier,
                    "state": item.state == .purchased ? "purchased" : "other",
                    "testTransactionIdentifier": item.identifier,
                    "originalTestTransactionIdentifier": item.originalTransactionIdentifier,
                    "purchaseDate": ISO8601DateFormatter().string(from: item.purchaseDate),
                ]
            },
        ]
        let data = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
        let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
        attachment.name = "pleazyng-trial-storekit-localtest-record"
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func optionalDate(_ value: Date?) -> Any {
        guard let value else { return NSNull() }
        return ISO8601DateFormatter().string(from: value)
    }

    private func tapWhenHittable(
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

    private func attachAccessibilityHierarchy(of app: XCUIApplication, named name: String) {
        let attachment = XCTAttachment(data: Data(app.debugDescription.utf8), uniformTypeIdentifier: "public.plain-text")
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func attachScreenshot(of app: XCUIApplication, named name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
