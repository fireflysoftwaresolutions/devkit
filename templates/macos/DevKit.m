// Copyright (c) 2026 Firefly Software Solutions Inc.
// SPDX-License-Identifier: Apache-2.0
// Native application identity for notifications; no URL handlers or acknowledgement actions.
#import <Cocoa/Cocoa.h>
#import <UserNotifications/UserNotifications.h>

@interface NotificationDelegate : NSObject <UNUserNotificationCenterDelegate>
@end
@implementation NotificationDelegate
- (void)userNotificationCenter:(UNUserNotificationCenter *)center
      willPresentNotification:(UNNotification *)notification
        withCompletionHandler:(void (^)(UNNotificationPresentationOptions))handler {
    handler(UNNotificationPresentationOptionAlert);
}
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSBundle *bundle = NSBundle.mainBundle;
        if (argc == 2 && strcmp(argv[1], "--check") == 0) {
            NSDictionary *identity = @{@"identifier": bundle.bundleIdentifier ?: @"",
                @"name": [bundle objectForInfoDictionaryKey:@"CFBundleDisplayName"] ?: @"",
                @"icon": @([bundle pathForResource:@"DevKit" ofType:@"icns"] != nil)};
            NSData *data = [NSJSONSerialization dataWithJSONObject:identity options:0 error:nil];
            fwrite(data.bytes, 1, data.length, stdout);
            return 0;  // No application registration or notification APIs in this validation mode.
        }
        if (argc != 2 || strcmp(argv[1], "--notify") != 0) return 64;
        NSData *data = [NSFileHandle.fileHandleWithStandardInput readDataOfLength:4097];
        if (data.length > 4096) return 64;
        id payload = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
        if (![payload isKindOfClass:NSDictionary.class]) return 64;
        id identifier = payload[@"id"];
        if (![identifier isKindOfClass:NSString.class] || [identifier length] != 64 ||
            [identifier rangeOfCharacterFromSet:[NSCharacterSet characterSetWithCharactersInString:
                @"0123456789abcdef"].invertedSet].location != NSNotFound) return 64;
        NSDictionary *bodies = @{
            @"crew": @"A crew message is waiting. Open dev crew inbox to read it.",
            @"peer": @"A session message is waiting. Open dev session inbox to read it.",
            @"decision": @"A decision is waiting. Open dev crew decisions to review it.",
            @"security": @"A security notice is available. Open dev audit to review it.",
            @"progress": @"Your work has an update. Open dev status to review it."};
        id category = payload[@"category"];
        NSString *body = [category isKindOfClass:NSString.class] ? bodies[category] : nil;
        if (!body) body = bodies[@"progress"];
        [NSApplication sharedApplication];
        // The notification center's delegate is weak; retain it through the run loop even under optimization.
        __attribute__((objc_precise_lifetime)) NotificationDelegate *delegate = [NotificationDelegate new];
        UNUserNotificationCenter *center = UNUserNotificationCenter.currentNotificationCenter;
        center.delegate = delegate;
        __block BOOL finished = NO;
        __block int result = 3;
        void (^finish)(int) = ^(int status) {
            dispatch_async(dispatch_get_main_queue(), ^{ result = status; finished = YES; });
        };
        void (^submit)(void) = ^{
            UNMutableNotificationContent *content = [UNMutableNotificationContent new];
            content.title = @"DevKit";
            content.body = body;
            UNNotificationRequest *request = [UNNotificationRequest requestWithIdentifier:identifier
                content:content trigger:nil];
            [center addNotificationRequest:request withCompletionHandler:^(NSError *error) {
                finish(error ? 3 : 0);
            }];
        };
        // Recover a crash after native acceptance but before Python stored the attempt result.
        void (^deduplicate)(void) = ^{
            [center getDeliveredNotificationsWithCompletionHandler:^(NSArray<UNNotification *> *delivered) {
                for (UNNotification *item in delivered) {
                    if ([item.request.identifier isEqual:identifier]) { finish(0); return; }
                }
                [center getPendingNotificationRequestsWithCompletionHandler:^(NSArray<UNNotificationRequest *> *pending) {
                    for (UNNotificationRequest *item in pending) {
                        if ([item.identifier isEqual:identifier]) { finish(0); return; }
                    }
                    submit();
                }];
            }];
        };
        [center requestAuthorizationWithOptions:UNAuthorizationOptionAlert
            completionHandler:^(BOOL granted, NSError *error) {
                if (error || !granted) { finish(error ? 3 : 2); return; }
                deduplicate();
            }];
        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:4];
        while (!finished && deadline.timeIntervalSinceNow > 0) {
            [[NSRunLoop currentRunLoop] runMode:NSDefaultRunLoopMode beforeDate:deadline];
        }
        return result;
    }
}
